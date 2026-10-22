/**
 * ITAMbox — Audit Basket for bulk-audit sessions.
 *
 * Accumulates assets into a basket via:
 *   - camera scan (mobile) — reuses the shared AssetScanner, kept open for rapid multi-scan;
 *   - USB barcode scanner (desktop) — emulates a keyboard, Enter-terminated;
 *   - manual typing.
 *
 * Each accepted code is validated server-side (without recording) and rendered
 * as a row with its classification (matched, mismatch, surprise). Committing
 * submits the batch via HTMX to be verified in a single transaction.
 */
import { AssetScanner } from './scanner';
import { beepFail, beepOk, createNotifier } from './scan-feedback';

interface AuditScanPayload {
  found: boolean;
  /** True on the 404 payload when the code is an EAN matching several assets. */
  ambiguous?: boolean;
  pk: number;
  label: string;
  asset_tag: string;
  serial: string;
  status: string;
  classification: 'matched' | 'mismatch' | 'surprise';
  observed_location: string;
  eligible: boolean;
  warning: string | null;
}

const notify = createNotifier('audit-scanner-modal', 'audit-scan-feedback');

function initAuditBasket(): void {
  const root = document.getElementById('audit-basket-root');
  if (!root || root.dataset.basketInitialized) return;
  root.dataset.basketInitialized = 'true';

  const validateUrl = root.dataset.validateUrl || '';

  const form = document.getElementById('audit-basket-form') as HTMLFormElement | null;
  const tbody = document.getElementById('audit-basket-rows');
  const template = document.getElementById('audit-basket-row-template') as HTMLTemplateElement | null;
  const input = document.getElementById('audit-basket-input') as HTMLInputElement | null;
  const countEl = document.getElementById('audit-basket-count');
  const emptyEl = document.getElementById('audit-basket-empty');
  const clearBtn = document.getElementById('audit-basket-clear') as HTMLButtonElement | null;
  const submitBtn = document.getElementById('audit-basket-submit') as HTMLButtonElement | null;

  if (!form || !tbody || !template) return;

  const basket = new Set<number>();

  function updateState(): void {
    const count = basket.size;
    if (countEl) countEl.textContent = String(count);
    const overlayCount = document.getElementById('audit-scanner-count');
    if (overlayCount) overlayCount.textContent = String(count);
    if (emptyEl) emptyEl.classList.toggle('d-none', count !== 0);
    if (clearBtn) clearBtn.disabled = count === 0;
    if (submitBtn) submitBtn.disabled = count === 0;
  }

  function flashRow(pk: number): void {
    const existing = tbody!.querySelector<HTMLElement>(`tr[data-pk="${pk}"]`);
    if (!existing) return;
    existing.classList.add('table-active');
    setTimeout(() => existing.classList.remove('table-active'), 700);
  }

  function renderRow(p: AuditScanPayload): void {
    const frag = template!.content.cloneNode(true) as DocumentFragment;
    const tr = frag.querySelector('tr') as HTMLElement;
    tr.dataset.pk = String(p.pk);

    const set = (field: string, value: string) => {
      const el = tr.querySelector<HTMLElement>(`[data-field="${field}"]`);
      if (el) el.textContent = value;
    };

    const pkInput = tr.querySelector<HTMLInputElement>('input[data-field="pk"]');
    if (pkInput) pkInput.value = String(p.pk);

    set('asset_tag', p.asset_tag || `#${p.pk}`);
    set('label', p.label);

    const badge = tr.querySelector<HTMLElement>('[data-field="classification"]');
    if (badge) {
      if (p.classification === 'matched') {
        badge.textContent = gettext('Matching');
        badge.className = 'badge bg-success-lt text-success';
      } else if (p.classification === 'mismatch') {
        badge.textContent = gettext('Mismatch');
        badge.className = 'badge bg-warning-lt text-warning';
      } else {
        badge.textContent = gettext('Surprise');
        badge.className = 'badge bg-orange-lt text-orange';
      }
    }

    if (p.warning) {
      tr.classList.add('table-warning');
      const warningSpan = document.createElement('span');
      warningSpan.className = 'badge bg-warning-lt text-warning d-block mt-1';
      warningSpan.textContent = p.warning;
      const cell = tr.querySelector('td');
      if (cell) cell.appendChild(warningSpan);
    }

    tbody!.appendChild(tr);
  }

  let cameraScanner: AssetScanner | null = null;

  function isCurrentCameraAction(sessionGeneration: number | undefined): boolean {
    return sessionGeneration === undefined
      || (cameraScanner !== null && cameraScanner.isSessionCurrent(sessionGeneration));
  }

  /**
   * Validate one code and fold it into the basket. Returns the round-trip so the
   * camera scanner can hold its gate open until this settles; USB and manual
   * entry ignore the result and stay ungated — one deliberate event per code.
   */
  function addByCode(code: string, sessionGeneration?: number): Promise<void> {
    const cleaned = (code || '').trim();
    if (!cleaned) return Promise.resolve();

    const url = `${validateUrl}?code=${encodeURIComponent(cleaned)}`;
    return fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(async (res) => {
        if (res.status === 403) throw new Error('forbidden');
        // Read the JSON body even on a 404: the resolver marks an EAN that
        // matches several assets (ambiguous) distinctly from a genuine miss,
        // and that distinction must reach the operator's message below.
        const body = (await res.json().catch(() => null)) as AuditScanPayload | null;
        if (!res.ok) {
          if (body?.ambiguous) throw new Error('ambiguous');
          throw new Error('not_found');
        }
        if (!body) throw new Error('not_found');
        return body;
      })
      .then((data: AuditScanPayload) => {
        if (!isCurrentCameraAction(sessionGeneration)) return;
        if (!data.found) throw new Error('not_found');
        if (basket.has(data.pk)) {
          beepFail();
          flashRow(data.pk);
          notify(interpolate(gettext('Already in basket: %(code)s'), { code: cleaned }, true), 'warn');
          return;
        }

        if (!data.eligible) {
          beepFail();
          notify(data.warning || gettext('This asset cannot be audited.'), 'fail');
          return;
        }

        basket.add(data.pk);
        renderRow(data);
        updateState();
        beepOk();
        notify(
          data.warning || interpolate(gettext('Added: %(label)s'), { label: data.asset_tag || data.label }, true),
          data.warning ? 'warn' : 'ok',
        );
      })
      .catch((err: Error) => {
        if (!isCurrentCameraAction(sessionGeneration)) return;
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

  if (input) {
    input.addEventListener('keydown', (event: KeyboardEvent) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const value = input.value;
      input.value = '';
      addByCode(value);
    });
  }

  if (document.getElementById('audit-open-scanner-btn')) {
    cameraScanner = new AssetScanner({
      readerId: 'audit-scanner-reader',
      modalId: 'audit-scanner-modal',
      torchId: 'audit-toggle-torch-btn',
      openBtnId: 'audit-open-scanner-btn',
      closeBtnId: 'audit-close-scanner-btn',
      errorDivId: 'audit-scanner-error',
      onResult(code: string, sessionGeneration: number) {
        return addByCode(code, sessionGeneration);
      },
    });
  }

  tbody.addEventListener('click', (event) => {
    const btn = (event.target as HTMLElement).closest('.audit-basket-remove');
    if (!btn) return;
    const tr = btn.closest<HTMLElement>('tr.audit-basket-row');
    if (!tr) return;
    const pk = Number(tr.dataset.pk);
    basket.delete(pk);
    tr.remove();
    updateState();
  });

  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      basket.clear();
      tbody.innerHTML = '';
      updateState();
      if (input) input.focus();
    });
  }

  // No auditCommitSuccess listener here. Committing swaps the whole
  // #reconciliation-container (hx-swap="outerHTML"), which re-renders a fresh,
  // empty basket and re-runs init — so the swap IS the reset. Registering a
  // document-level listener inside init would stack a new handler (closing over
  // the now-detached basket) on every commit — a listener leak.
  updateState();
  if (input) input.focus();
}

document.addEventListener('DOMContentLoaded', initAuditBasket);
document.body.addEventListener('htmx:afterSettle', initAuditBasket);
