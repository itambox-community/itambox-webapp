/**
 * ITAMbox — Scan basket for bulk check-in / bulk disposal.
 *
 * Accumulates assets into a basket via three inputs that all funnel through
 * `addByCode()`:
 *   - camera scan (mobile) — reuses the shared AssetScanner, kept open for rapid
 *     multi-scan;
 *   - USB barcode scanner (desktop) — emulates a keyboard, Enter-terminated;
 *   - manual typing.
 *
 * Each accepted code is resolved server-side (tenant-scoped, eligibility-checked)
 * and rendered as a row inside #scan-basket-form. Rows carry the hidden `pk`
 * inputs (and, in disposal mode, per-row `proceeds_<pk>` inputs) so a plain form
 * POST submits the whole batch. Success/failure reuse the audit beep events.
 */
import { AssetScanner } from './scanner';
import { beepFail, beepOk, createNotifier } from './scan-feedback';

interface ScanPayload {
  found: boolean;
  /** True on the 404 payload when the code is an EAN matching several assets. */
  ambiguous?: boolean;
  pk: number;
  tenant_id: number;
  label: string;
  asset_tag: string;
  serial: string;
  status: string;
  assigned_to: string;
  book_value: string | null;
  eligible: boolean;
  warning: string | null;
}

interface BasketEntry {
  payload: ScanPayload;
  proceeds: string;
}

const notify = createNotifier('basket-scanner-modal', 'basket-scan-feedback');

function initScanBasket(): void {
  const root = document.getElementById('scan-basket-root');
  if (!root || root.dataset.basketInitialized) return;
  root.dataset.basketInitialized = 'true';

  const mode = root.dataset.mode || 'checkin';
  const resolveUrl = root.dataset.resolveUrl || '';

  const form = document.getElementById('scan-basket-form') as HTMLFormElement | null;
  const tbody = document.getElementById('scan-basket-rows');
  const template = document.getElementById('scan-basket-row-template') as HTMLTemplateElement | null;
  const input = document.getElementById('scan-basket-input') as HTMLInputElement | null;
  const countEl = document.getElementById('scan-basket-count');
  const emptyEl = document.getElementById('scan-basket-empty');
  const clearBtn = document.getElementById('scan-basket-clear') as HTMLButtonElement | null;
  const submitBtn = document.getElementById('scan-basket-submit') as HTMLButtonElement | null;
  if (!form || !tbody || !template) return;

  const tenantField = form.querySelector<HTMLSelectElement | HTMLInputElement>('[name="tenant"]');
  const tenantRequired = !!tenantField && tenantField.type !== 'hidden';
  const cameraBtn = document.getElementById('basket-open-scanner-btn') as HTMLButtonElement | null;

  const baskets = new Map<number, Map<number, BasketEntry>>();
  let concreteTenantId = tenantRequired ? 0 : normalizeTenantId(tenantField?.value);

  const keptAsideNotice = document.createElement('div');
  keptAsideNotice.id = 'scan-basket-kept-aside';
  keptAsideNotice.className = 'alert alert-info small mb-3 d-none';
  keptAsideNotice.setAttribute('role', 'status');
  keptAsideNotice.hidden = true;
  root.insertBefore(keptAsideNotice, form);

  function normalizeTenantId(value: unknown): number {
    const parsed = typeof value === 'number' ? value : Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : 0;
  }

  function selectTenant(tenantId: number): void {
    if (!tenantField) return;
    const value = String(tenantId);
    tenantField.value = value;
    const tomSelect = (tenantField as HTMLSelectElement & {
      tomselect?: { setValue: (selected: string) => void };
    }).tomselect;
    tomSelect?.setValue(value);
  }

  function activeTenantId(): number {
    return tenantRequired ? normalizeTenantId(tenantField?.value) : concreteTenantId;
  }

  function basketFor(tenantId: number, create = false): Map<number, BasketEntry> | undefined {
    let basket = baskets.get(tenantId);
    if (!basket && create) {
      basket = new Map<number, BasketEntry>();
      baskets.set(tenantId, basket);
    }
    return basket;
  }

  function activeBasket(): Map<number, BasketEntry> | undefined {
    const tenantId = activeTenantId();
    if (tenantRequired && tenantId === 0) return undefined;
    return basketFor(tenantId);
  }

  function updateKeptAsideNotice(): void {
    const tenantId = activeTenantId();
    let keptAsideCount = 0;
    if (tenantRequired && tenantId !== 0) {
      baskets.forEach((basket, basketTenantId) => {
        if (basketTenantId !== tenantId) keptAsideCount += basket.size;
      });
    }

    const visible = keptAsideCount > 0;
    keptAsideNotice.hidden = !visible;
    keptAsideNotice.classList.toggle('d-none', !visible);
    keptAsideNotice.textContent = visible
      ? interpolate(
        ngettext(
          '%(count)s asset from another tenant is kept aside. Switch the target tenant to see it.',
          '%(count)s assets from another tenant are kept aside. Switch the target tenant to see them.',
          keptAsideCount,
        ),
        { count: keptAsideCount },
        true,
      )
      : '';
  }

  function updateState(): void {
    const count = activeBasket()?.size || 0;
    if (countEl) countEl.textContent = String(count);
    const overlayCount = document.getElementById('basket-scanner-count');
    if (overlayCount) overlayCount.textContent = String(count);
    if (emptyEl) emptyEl.classList.toggle('d-none', count !== 0);
    if (clearBtn) clearBtn.disabled = count === 0;
    const tenantSelected = !tenantRequired || !!tenantField?.value;
    if (input) input.disabled = !tenantSelected;
    if (cameraBtn) cameraBtn.disabled = !tenantSelected;
    if (submitBtn) submitBtn.disabled = count === 0 || !tenantSelected;
    const disposeConfirmText = document.getElementById('scan-basket-dispose-confirm-text');
    if (disposeConfirmText) {
      disposeConfirmText.textContent = interpolate(
        ngettext('Dispose of %(count)s asset?', 'Dispose of %(count)s assets?', count),
        { count },
        true,
      );
    }
    updateKeptAsideNotice();
  }

  function flashRow(pk: number): void {
    const existing = tbody!.querySelector<HTMLElement>(`tr[data-pk="${pk}"]`);
    if (!existing) return;
    existing.classList.add('table-active');
    setTimeout(() => existing.classList.remove('table-active'), 700);
  }

  function renderRow(entry: BasketEntry): void {
    const p = entry.payload;
    const frag = template!.content.cloneNode(true) as DocumentFragment;
    const tr = frag.querySelector('tr') as HTMLElement;
    tr.dataset.pk = String(p.pk);
    tr.dataset.tenantId = String(normalizeTenantId(p.tenant_id));

    const set = (field: string, value: string) => {
      const el = tr.querySelector<HTMLElement>(`[data-field="${field}"]`);
      if (el) el.textContent = value;
    };

    const pkInput = tr.querySelector<HTMLInputElement>('input[data-field="pk"]');
    if (pkInput) pkInput.value = String(p.pk);

    set('asset_tag', p.asset_tag || `#${p.pk}`);
    set('label', p.label);
    set('status', p.status || gettext('Not set'));
    set('assigned_to', p.assigned_to || gettext('Not set'));
    set('book_value', p.book_value || gettext('Not set'));

    const proceeds = tr.querySelector<HTMLInputElement>('input[data-field="proceeds"]');
    if (proceeds) {
      proceeds.name = `proceeds_${p.pk}`;
      proceeds.value = entry.proceeds;
      proceeds.addEventListener('input', () => {
        entry.proceeds = proceeds.value;
      });
      // Book value is the depreciated accounting residual, NOT money received.
      // Show it only as a placeholder hint — never as the submitted value — so a
      // blank field correctly means "no proceeds" and dispose_asset() freezes the
      // residual into disposal_value itself.
      if (p.book_value) proceeds.placeholder = p.book_value;
    }

    const warn = tr.querySelector<HTMLElement>('[data-field="warning"]');
    if (warn && p.warning) {
      warn.textContent = p.warning;
      warn.hidden = false;
      tr.classList.add('table-warning');
    }

    tbody!.appendChild(tr);
  }

  function renderActiveBasket(): void {
    tbody!.innerHTML = '';
    activeBasket()?.forEach((entry) => renderRow(entry));
  }

  let cameraScanner: AssetScanner | null = null;

  function isCurrentCameraAction(sessionGeneration: number | undefined): boolean {
    return sessionGeneration === undefined
      || (cameraScanner !== null && cameraScanner.isSessionCurrent(sessionGeneration));
  }

  /**
   * Resolve one code and fold it into the basket. Returns the round-trip so the
   * camera scanner can hold its gate open until this settles; USB and manual
   * entry ignore the result and stay ungated — one deliberate event per code.
   */
  function addByCode(code: string, sessionGeneration?: number): Promise<void> {
    const cleaned = (code || '').trim();
    if (!cleaned) return Promise.resolve();

    if (tenantRequired && !tenantField?.value) {
      notify(gettext('Select a target tenant before scanning.'), 'warn');
      return Promise.resolve();
    }

    const tenantQuery = tenantRequired || tenantField?.value
      ? `&tenant=${encodeURIComponent(tenantField?.value || '')}`
      : '';
    const url = `${resolveUrl}?code=${encodeURIComponent(cleaned)}&mode=${encodeURIComponent(mode)}${tenantQuery}`;
    return fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(async (res) => {
        if (res.status === 403) throw new Error('forbidden');
        // Read the JSON body even on a 404: the resolver marks an EAN that
        // matches several assets (ambiguous) distinctly from a genuine miss,
        // and that distinction must reach the operator's message below.
        const body = (await res.json().catch(() => null)) as ScanPayload | null;
        if (!res.ok) {
          if (body?.ambiguous) throw new Error('ambiguous');
          throw new Error('not_found');
        }
        if (!body) throw new Error('not_found');
        return body;
      })
      .then((data: ScanPayload) => {
        if (!isCurrentCameraAction(sessionGeneration)) return;
        if (!data.found) throw new Error('not_found');
        const tenantId = normalizeTenantId(data.tenant_id);
        if (!tenantRequired && concreteTenantId === 0) concreteTenantId = tenantId;
        const basket = basketFor(tenantId, true)!;
        if (basket.has(data.pk)) {
          beepFail();
          flashRow(data.pk);
          notify(interpolate(gettext('Already in basket: %(code)s'), { code: cleaned }, true), 'warn');
          return;
        }
        const entry = { payload: data, proceeds: '' };
        basket.set(data.pk, entry);
        if (tenantId === activeTenantId()) renderRow(entry);
        updateState();
        beepOk();
        notify(
          data.warning || interpolate(gettext('Added: %(label)s'), { label: data.asset_tag || data.label }, true),
          data.warning ? 'warn' : 'ok',
        );
      })
      .catch((err: Error) => {
        if (!isCurrentCameraAction(sessionGeneration)) return;
        // Resolves rather than re-throws: the failure is reported here, and the
        // camera gate re-arms on settle so the code can be presented again once
        // its duplicate window has passed. USB/manual retries are ungated.
        beepFail();
        if (err.message === 'forbidden') {
          notify(gettext('You do not have permission to do this.'), 'fail');
        } else if (err.message === 'ambiguous') {
          notify(
            interpolate(
              gettext("'%(code)s' matches several assets. Scan the asset's label instead."),
              { code: cleaned },
              true,
            ),
            'fail',
          );
        } else {
          notify(interpolate(gettext('No asset matches: %(code)s'), { code: cleaned }, true), 'fail');
        }
      });
  }

  // ── USB scanner / manual entry: Enter terminates a scan ──
  if (input) {
    input.addEventListener('keydown', (event: KeyboardEvent) => {
      if (event.key !== 'Enter') return;
      event.preventDefault(); // never submit the form from the scan box
      const value = input.value;
      input.value = '';
      addByCode(value);
    });
  }

  if (tenantField && tenantRequired) {
    tenantField.addEventListener('change', () => {
      renderActiveBasket();
      updateState();
      input?.focus();
    });
  }

  // ── Camera scanner (kept open for rapid multi-scan) ──
  if (document.getElementById('basket-open-scanner-btn')) {
    cameraScanner = new AssetScanner({
      readerId: 'basket-scanner-reader',
      modalId: 'basket-scanner-modal',
      torchId: 'basket-toggle-torch-btn',
      openBtnId: 'basket-open-scanner-btn',
      closeBtnId: 'basket-close-scanner-btn',
      errorDivId: 'basket-scanner-error',
      onResult(code: string, sessionGeneration: number) {
        return addByCode(code, sessionGeneration);
      },
    });
  }

  // ── Remove a row ──
  tbody.addEventListener('click', (event) => {
    const btn = (event.target as HTMLElement).closest('.scan-basket-remove');
    if (!btn) return;
    const tr = btn.closest<HTMLElement>('tr.scan-basket-row');
    if (!tr) return;
    const pk = Number(tr.dataset.pk);
    const tenantId = normalizeTenantId(tr.dataset.tenantId);
    basketFor(tenantId)?.delete(pk);
    tr.remove();
    updateState();
  });

  // ── Clear all ──
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      activeBasket()?.clear();
      tbody.innerHTML = '';
      updateState();
      if (input) input.focus();
    });
  }

  // ── Disposal confirm modal → submit the form ──
  const confirmSubmit = document.getElementById('scan-basket-confirm-submit');
  if (confirmSubmit) {
    confirmSubmit.addEventListener('click', () => {
      if ((activeBasket()?.size || 0) > 0) form.submit();
    });
  }

  // ── Seed from server-rendered selection (list-view checkbox seeding) ──
  const seedEl = document.getElementById('scan-seed-data');
  if (seedEl && seedEl.textContent) {
    try {
      const seeds = JSON.parse(seedEl.textContent) as ScanPayload[];
      seeds.forEach((p) => {
        if (p && p.pk) {
          const tenantId = normalizeTenantId(p.tenant_id);
          const basket = basketFor(tenantId, true)!;
          if (!basket.has(p.pk)) basket.set(p.pk, { payload: p, proceeds: '' });
        }
      });

      const seededTenantIds = [...baskets.keys()];
      if (seededTenantIds.length === 1 && seededTenantIds[0] !== 0) {
        if (tenantRequired && tenantField && !tenantField.value) {
          selectTenant(seededTenantIds[0]);
        } else if (!tenantRequired && concreteTenantId === 0) {
          concreteTenantId = seededTenantIds[0];
        }
      }
    } catch (_e) {
      // malformed seed data — ignore, start with an empty basket
    }
  }

  renderActiveBasket();
  updateState();
  if (input) input.focus();
}

document.addEventListener('DOMContentLoaded', initScanBasket);
document.body.addEventListener('htmx:afterSettle', initScanBasket);
