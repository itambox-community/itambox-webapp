/**
 * ITAMbox Batch Actions — checkbox tracking, batch bar visibility, bulk ops.
 *
 * Selection is scoped to the nearest `.js-selection-scope` ancestor, so the same
 * logic drives the main object list and any table embedded in a detail-view tab.
 * Within a scope, `[data-select-all]` toggles every `pk` checkbox, the batch bar shows a
 * live count, and bulk action buttons collect the checked pks into their form.
 */
(function () {
  const SCOPE_SELECTOR = '.js-selection-scope';

  function pkCheckboxes(scope: HTMLElement): NodeListOf<HTMLInputElement> {
    return scope.querySelectorAll<HTMLInputElement>('input[type="checkbox"][name="pk"]');
  }

  function checkedPks(scope: HTMLElement): NodeListOf<HTMLInputElement> {
    return scope.querySelectorAll<HTMLInputElement>('input[type="checkbox"][name="pk"]:checked');
  }

  function updateScope(scope: HTMLElement): void {
    const boxes = pkCheckboxes(scope);
    const count = scope.querySelectorAll('input[type="checkbox"][name="pk"]:checked').length;
    const none = count === 0;

    // Legacy show/hide bars — still used by detail-view embedded tables.
    scope.querySelectorAll<HTMLElement>('.batch-actions-bar').forEach(function (bar) {
      bar.classList.toggle('d-none', none);
      bar.querySelectorAll<HTMLElement>('.fw-bold').forEach(function (el) {
        el.textContent = interpolate(gettext('%(count)s selected'), { count }, true);
      });
    });

    // Persistent NetBox-style toolbar — always visible; buttons disabled until
    // a selection exists, and a live count label.
    scope.querySelectorAll<HTMLElement>('.bulk-selected-count').forEach(function (el) {
      el.textContent = interpolate(gettext('%(count)s selected'), { count }, true);
    });
    scope.querySelectorAll<HTMLElement>('.bulk-action-btn').forEach(function (btn) {
      (btn as HTMLButtonElement).disabled = none;
      btn.classList.toggle('disabled', none);
      btn.setAttribute('aria-disabled', none ? 'true' : 'false');
    });

    const selectAllCbs = scope.querySelectorAll<HTMLInputElement>('[data-select-all]');
    selectAllCbs.forEach(function (selectAllCb) {
      selectAllCb.checked = boxes.length > 0 && count === boxes.length;
      selectAllCb.indeterminate = count > 0 && count < boxes.length;
      selectAllCb.disabled = boxes.length === 0;
      selectAllCb.setAttribute('aria-disabled', boxes.length === 0 ? 'true' : 'false');
    });
  }

  function updateAllScopes(): void {
    document.querySelectorAll<HTMLElement>(SCOPE_SELECTOR).forEach(updateScope);
  }

  function initBulkEditSelectors(): void {
    const selectors = document.querySelectorAll<HTMLInputElement>('.bulk-edit-selector');
    selectors.forEach(function (cb) {
      const fieldName = cb.value;
      const container = document.getElementById('field_container_' + fieldName);
      if (container) {
        container.classList.toggle('d-none', !cb.checked);
      }
    });
  }

  /** Collect the checked pks within a scope into hidden inputs on the given form. */
  function gatherPks(form: HTMLFormElement, scope: HTMLElement): boolean {
    const checked = checkedPks(scope);
    if (checked.length === 0) {
      alert(gettext('No items selected.'));
      return false;
    }
    let container = form.querySelector<HTMLElement>('.bulk-pks-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'bulk-pks-container d-inline';
      form.appendChild(container);
    }
    container.innerHTML = '';
    checked.forEach(function (cb) {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'pk';
      input.value = cb.value;
      container!.appendChild(input);
    });
    return true;
  }

  document.addEventListener('change', function (event) {
    const target = event.target as HTMLInputElement;
    if (!target || target.type !== 'checkbox') return;

    if (target.classList.contains('bulk-edit-selector')) {
      const fieldName = target.value;
      const container = document.getElementById('field_container_' + fieldName);
      if (container) {
        container.classList.toggle('d-none', !target.checked);
      }
      return;
    }

    const scope = target.closest<HTMLElement>(SCOPE_SELECTOR);
    if (!scope) return;

    if (target.name === 'pk') {
      updateScope(scope);
    } else if (target.matches('[data-select-all]') || target.name === 'select_all') {
      pkCheckboxes(scope).forEach(function (cb) {
        cb.checked = target.checked;
      });
      updateScope(scope);
    }
  });

  document.body.addEventListener('htmx:afterSettle', function () {
    updateAllScopes();
    initBulkEditSelectors();
  });

  // Initial run
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      updateAllScopes();
      initBulkEditSelectors();
    });
  } else {
    updateAllScopes();
    initBulkEditSelectors();
  }

  // Bulk Checkout modal submit handler. The modal form lives outside any
  // selection scope, so it reads from the list container.
  document.addEventListener('submit', function (event) {
    const target = event.target as HTMLElement;
    const form = target.closest<HTMLFormElement>('#bulk-checkout-inventory-form');
    if (!form) return;

    const checkboxes = document.querySelectorAll<HTMLInputElement>(
      '#object-list-table-container input[type="checkbox"][name="pk"]',
    );
    const pks: string[] = [];
    checkboxes.forEach(function (cb) {
      if (cb.checked) pks.push(cb.value);
    });

    if (pks.length === 0) {
      event.preventDefault();
      alert(gettext('No items selected.'));
      return;
    }

    const containerId = 'bulk-checkout-inventory-pks';
    let container = form.querySelector<HTMLElement>('#' + containerId);
    if (!container) {
      container = document.createElement('div');
      container.id = containerId;
      form.appendChild(container);
    }
    container.innerHTML = '';
    pks.forEach(function (pk) {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'pk';
      input.value = pk;
      container.appendChild(input);
    });
    // Let the event bubble naturally so HTMX or the browser processes the submit
  });


  // "Seed" buttons (Check-in / Dispose Selected) — collect the checked pks and
  // navigate to the scan-basket page with ?pk=... so the basket opens pre-filled
  // and the user can keep scanning more.
  document.addEventListener('click', function (event) {
    const btn = (event.target as HTMLElement).closest<HTMLElement>('.btn-bulk-scan-seed');
    if (!btn) return;
    event.preventDefault();
    const rawUrl = btn.getAttribute('data-scan-url');
    if (!rawUrl) return;
    const checked = document.querySelectorAll<HTMLInputElement>(
      '#object-list-table-container input[type="checkbox"][name="pk"]:checked',
    );
    if (checked.length === 0) {
      alert(gettext('No items selected.'));
      return;
    }
    // Parse the DOM-supplied URL and enforce an http(s) allowlist before navigating,
    // building the query via the URL API rather than string concatenation.
    let target: URL;
    try {
      target = new URL(rawUrl, window.location.origin);
    } catch {
      return;
    }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') return;
    checked.forEach(function (cb) {
      target.searchParams.append('pk', cb.value);
    });
    window.location.href = target.toString();
  });

  // Delegated click handler for bulk delete/edit/restore/purge, scoped to the
  // selection container the button lives in. Aligns with strict CSP (no inline JS).
  const BULK_BUTTONS: Array<{ trigger: string; form: string; confirm?: string }> = [
    { trigger: '.btn-bulk-delete', form: '.bulk-delete-form' },
    { trigger: '.btn-bulk-edit', form: '.bulk-edit-form' },
    { trigger: '.btn-bulk-label', form: '.bulk-label-form' },
    { trigger: '.btn-bulk-restore', form: '.bulk-restore-form' },
    { trigger: '.btn-bulk-acknowledge', form: '.bulk-acknowledge-form' },
    { trigger: '.btn-bulk-resolve', form: '.bulk-resolve-form' },
    { trigger: '.btn-bulk-receive', form: '.bulk-receive-form' },
    {
      trigger: '.btn-bulk-purge',
      form: '.bulk-purge-form',
      confirm: gettext('Are you sure you want to PERMANENTLY delete the selected items? This cannot be undone!'),
    },
  ];

  document.addEventListener('click', function (event) {
    const target = event.target as HTMLElement;

    for (const spec of BULK_BUTTONS) {
      const btn = target.closest(spec.trigger);
      if (!btn) continue;

      event.preventDefault();
      const scope = btn.closest<HTMLElement>(SCOPE_SELECTOR);
      if (!scope) return;
      const form = scope.querySelector<HTMLFormElement>(spec.form);
      if (!form) return;
      if (spec.confirm && !confirm(spec.confirm)) return;
      if (gatherPks(form, scope)) {
        form.submit();
      }
      return;
    }
  });
})();
