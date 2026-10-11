/**
 * ITAMbox — Object Detail Tab and Refresh Controller.
 *
 * Tab activation rules (see scratch/TAB_CONVENTIONS.md):
 *  - The active tab is driven by the user's click (Bootstrap) and, on a fresh
 *    page load / boosted main-nav, by the `?tab=` query param.
 *  - We DO NOT re-apply `?tab=` on every `htmx:afterSettle`. A lazy tab pane
 *    loads its table with `hx-get`; that swap must never re-select a tab, or a
 *    slow load landing after the user has moved on would yank the active tab
 *    backwards (the "tab-load race"). `showTabFromUrl` therefore runs only when
 *    the main content wrapper itself is (re)rendered by a navigation.
 */
(function () {
  const MAIN = 'page-content-wrapper';

  function findTab(tabName: string): HTMLElement | null {
    return (
      Array.from(document.querySelectorAll('a')).find((tabEl) => {
        const dataTarget = tabEl.getAttribute('data-bs-target');
        const href = tabEl.getAttribute('href');
        const hxGet = tabEl.getAttribute('hx-get');
        return (
          dataTarget === `#${tabName}` ||
          href === `#${tabName}` ||
          href === `?tab=${tabName}` ||
          hxGet === `?tab=${tabName}`
        );
      }) || null
    );
  }

  /** Show the tab named in `?tab=` — full load / boosted main-nav only. */
  function showTabFromUrl() {
    const tabParam = new URLSearchParams(window.location.search).get('tab');
    if (!tabParam) return;
    const tabEl = findTab(tabParam);
    if (!tabEl) return;
    // Only call .show() when the tab is not already the active one. The server
    // renders the correct tab active on a full load / boosted nav, so re-showing
    // it would replay the Bootstrap `.fade` for no reason (a flicker). We still
    // fall through to loadLazyPaneIfNeeded so an active-but-unloaded lazy pane
    // (deep link to ?tab=<lazy>) gets its content.
    if (!tabEl.classList.contains('active')) {
      try {
        (bootstrap as any).Tab.getOrCreateInstance(tabEl).show();
      } catch (_e) {
        tabEl.click();
        return;
      }
    }
    loadLazyPaneIfNeeded(tabEl);
  }

  /**
   * Lazy tab panes fire their `hx-get` on `click` only, so a direct visit or a
   * boosted nav to `?tab=<lazy>` would otherwise show an endless spinner. If the
   * activated tab is lazy and its pane is still the placeholder, kick its load.
   */
  function loadLazyPaneIfNeeded(tabEl: HTMLElement) {
    if (!tabEl.getAttribute('hx-get')) return;
    const paneSel = tabEl.getAttribute('data-bs-target') || tabEl.getAttribute('hx-target');
    const pane = paneSel ? (document.querySelector(paneSel) as HTMLElement | null) : null;
    if (pane && pane.querySelector('.spinner-border') && typeof htmx !== 'undefined') {
      (htmx as any).trigger(tabEl, 'click');
    }
  }

  /** Keep the URL in sync with the active tab (user clicks + programmatic shows). */
  document.body.addEventListener('shown.bs.tab', function (e: any) {
    if (!e.target) return;
    let tabName: string | null = null;
    const dataTarget = e.target.getAttribute('data-bs-target');
    const targetHref = e.target.getAttribute('href');
    if (dataTarget && dataTarget.startsWith('#')) {
      tabName = dataTarget.substring(1);
    } else if (targetHref && targetHref.startsWith('#')) {
      tabName = targetHref.substring(1);
    } else if (targetHref && targetHref.startsWith('?')) {
      tabName = new URLSearchParams(targetHref).get('tab');
    }
    if (!tabName) return;

    const newUrl = new URL(window.location.href);
    if (tabName === 'details') {
      newUrl.searchParams.delete('tab');
    } else {
      newUrl.searchParams.set('tab', tabName);
    }
    // replaceState (not pushState): tabs are view state, not history entries —
    // pushing per-click polluted history and fed the old afterSettle race loop.
    if (newUrl.toString() !== window.location.href) {
      window.history.replaceState({}, '', newUrl.toString());
    }
    // keep the just-shown tab visible if the bar is horizontally scrolled
    scrollTabIntoView(e.target as HTMLElement, 'smooth');
  });

  function refreshDetailsPage() {
    // The event is shared with list-level actions. Lists already declare their
    // own HTMX refresh trigger; the detail controller must not start a second
    // body refresh against the partial wrapper.
    if (document.getElementById('object-list-dynamic-content')) {
      return;
    }
    if (typeof htmx !== 'undefined') {
      htmx.ajax('GET', window.location.href, { target: 'body', swap: 'outerHTML' });
    } else {
      window.location.reload();
    }
  }

  document.body.addEventListener('tableRefreshRequired', refreshDetailsPage);
  document.body.addEventListener('licenseUpdated', refreshDetailsPage);

  /** Smoothly scroll a tab into view within its (overflowing) tab bar. */
  function scrollTabIntoView(tab: HTMLElement, behavior: ScrollBehavior) {
    const bar = tab.closest('.page-header-tabs') as HTMLElement | null;
    if (!bar) return;
    const tabRect = tab.getBoundingClientRect();
    const barRect = bar.getBoundingClientRect();
    const delta = tabRect.left - barRect.left - (bar.clientWidth - tabRect.width) / 2;
    if (Math.abs(delta) > 1) bar.scrollBy({ left: delta, behavior });
  }

  /**
   * Horizontal scroll affordance for the tab bar. The bar scrolls when tabs
   * overflow but the scrollbar is hidden, so toggle `has-scroll-start` /
   * `has-scroll-end` (on the bar and its host) to drive the edge-fade + chevron
   * hint, and bring the active tab into view. Idempotent per bar.
   */
  function initTabScroll() {
    document.querySelectorAll('.page-header-tabs').forEach(function (node) {
      const bar = node as HTMLElement & {
        _tabScrollWired?: boolean;
        _tabScrollUpdate?: () => void;
        _tabScrollRO?: ResizeObserver;
      };
      if (bar._tabScrollWired) {
        if (bar._tabScrollUpdate) bar._tabScrollUpdate();
        return;
      }
      bar._tabScrollWired = true;
      const host = bar.parentElement;
      if (host) host.classList.add('tab-scroll-host');
      const update = function () {
        const max = bar.scrollWidth - bar.clientWidth;
        const atStart = bar.scrollLeft > 1;
        const atEnd = bar.scrollLeft < max - 1;
        bar.classList.toggle('has-scroll-start', atStart);
        bar.classList.toggle('has-scroll-end', atEnd);
        if (host) {
          host.classList.toggle('has-scroll-start', atStart);
          host.classList.toggle('has-scroll-end', atEnd);
        }
      };
      bar._tabScrollUpdate = update;
      bar.addEventListener('scroll', update, { passive: true });
      if (typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(update);
        ro.observe(bar);
        bar._tabScrollRO = ro; // referenced from the element so it lives/dies with it
      }
      const active = bar.querySelector('.nav-link.active') as HTMLElement | null;
      if (active) scrollTabIntoView(active, 'auto');
      update();
    });
  }

  /**
   * Apply `?tab=` only when the MAIN content wrapper is swapped by a (boosted)
   * navigation — never on a lazy tab-content swap (targets a pane) or an OOB
   * swap (targets <title>/#django-messages). This is the core race fix. The tab
   * bar is recreated on the same swap, so (re)wire its scroll affordance too.
   */
  function onSettle(evt: any) {
    if (evt && evt.detail && evt.detail.target && evt.detail.target.id === MAIN) {
      showTabFromUrl();
      initTabScroll();
    }
  }

  function init() {
    showTabFromUrl();
    initTabScroll();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  document.body.addEventListener('htmx:afterSettle', onSettle);
  window.addEventListener('resize', function () {
    document.querySelectorAll('.page-header-tabs').forEach(function (bar: any) {
      if (bar._tabScrollUpdate) bar._tabScrollUpdate();
    });
  });
})();
