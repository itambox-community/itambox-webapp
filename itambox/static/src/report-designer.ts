/**
 * ITAMbox — Report Template Designer and Column Sequence Manager.
 */
(function () {
  // Escape user-controlled text (column/field labels) before interpolating it
  // into innerHTML templates below — labels may originate from custom-field
  // names and could otherwise carry markup (stored XSS).
  function escapeHtml(value: string): string {
    return value.replace(/[&<>"']/g, (ch) => {
      switch (ch) {
        case '&': return '&amp;';
        case '<': return '&lt;';
        case '>': return '&gt;';
        case '"': return '&quot;';
        default: return '&#39;';
      }
    });
  }

  const initializedEditors = new WeakSet<HTMLElement>();

  function initReportTemplateForm(root: ParentNode = document) {
    const rootElement = root as HTMLElement;
    const reportEditor = root === document
      ? document.getElementById('report-template-editor')
      : rootElement.id === 'report-template-editor'
        ? rootElement
        : root.querySelector<HTMLElement>('#report-template-editor');
    const previewModal = document.getElementById('previewModal');
    if (!reportEditor || !previewModal || initializedEditors.has(reportEditor)) return;
    initializedEditors.add(reportEditor);

    const colsContainer = document.getElementById('div_id_included_columns');
    if (colsContainer) {
      const formChecks = Array.from(colsContainer.querySelectorAll('.form-check')) as HTMLElement[];
      if (formChecks.length > 0) {
        // Hide standard inputs
        formChecks.forEach(el => el.classList.add('d-none'));

        // Read saved sequence from the json_script element (autoescaped by Django).
        const savedSeqEl = document.getElementById('report-template-saved-sequence');
        let savedSeq: string[] = [];
        if (savedSeqEl && savedSeqEl.textContent) {
          try {
            const parsed = JSON.parse(savedSeqEl.textContent);
            if (Array.isArray(parsed)) {
              savedSeq = parsed;
            }
          } catch {
            savedSeq = [];
          }
        }

        if (savedSeq.length > 0) {
          const formChecksMap: Record<string, HTMLElement> = {};
          formChecks.forEach(checkDiv => {
            const input = checkDiv.querySelector('input');
            if (input) {
              formChecksMap[input.value] = checkDiv;
            }
          });
          // Append in correct order
          savedSeq.forEach(val => {
            const checkDiv = formChecksMap[val];
            if (checkDiv) {
              colsContainer.appendChild(checkDiv);
              delete formChecksMap[val];
            }
          });
          // Append any remaining elements
          Object.values(formChecksMap).forEach(checkDiv => {
            colsContainer.appendChild(checkDiv);
          });
        }

        let managerWrapper = document.getElementById('visual-cols-manager-wrapper');
        if (!managerWrapper) {
          managerWrapper = document.createElement('div');
          managerWrapper.id = 'visual-cols-manager-wrapper';
          managerWrapper.className = 'visual-cols-manager report-designer-manager mt-2 p-3 bg-body-secondary rounded-3 border';
          managerWrapper.innerHTML = `
              <div class="row">
                  <div class="col-md-6 mb-3 mb-md-0">
                      <div class="text-secondary small fw-bold mb-2 uppercase text-uppercase d-flex align-items-center">
                          <i class="mdi mdi-sort me-1 text-primary"></i>
                          ${gettext('Selected Sequence (Orderable)')}
                      </div>
                      <div id="active-cols-list" class="report-designer-active-list d-flex flex-column gap-2 p-2 bg-body rounded border">
                          <span class="text-muted small text-center my-auto py-3 italic">${gettext('No columns selected. Click available columns to add them.')}</span>
                      </div>
                  </div>
                  <div class="col-md-6">
                      <div class="text-secondary small fw-bold mb-2 uppercase text-uppercase">
                          ${gettext('Available Columns')}
                      </div>
                      <div id="available-cols-list" class="report-designer-available-list d-flex flex-wrap gap-2 p-2 bg-body rounded border">
                      </div>
                  </div>
              </div>
          `;
          colsContainer.appendChild(managerWrapper);
        }

        const activeList = managerWrapper.querySelector('#active-cols-list') as HTMLElement;
        const availableList = managerWrapper.querySelector('#available-cols-list') as HTMLElement;
        const reportTypeSelect = document.querySelector('select[name="report_type"]') as HTMLSelectElement | null;

        const columnsByReportType: Record<string, string[]> = {
          'asset_summary': [
            'asset_tag', 'name', 'manufacturer', 'model', 'serial_number',
            'status', 'location', 'assigned_to', 'purchase_cost',
            'purchase_date', 'warranty_months'
          ],
          'license_utilization': [
            'license_name', 'software', 'seats', 'assigned_seats',
            'available_seats', 'utilization_rate'
          ],
          'subscription_renewals': [
            'subscription_name', 'supplier', 'agreement_entitled_quantity', 'billing_cycle', 'cost', 'end_date'
          ],
          'asset_maintenance': [
            'maintenance_asset', 'maintenance_type',
            'maintenance_status', 'maintenance_cost', 'maintenance_start_date',
            'maintenance_completion_date', 'maintenance_downtime'
          ],
          'asset_depreciation': [
            'asset_tag', 'name', 'purchase_cost', 'salvage_value', 'depreciation_months', 'current_value'
          ],
          'software_inventory': [
            'software_name', 'manufacturer', 'version', 'category', 'license_type', 'installed_count', 'license_count'
          ],
          'contract_renewals': ['contract_number', 'contract_name', 'contract_type', 'contract_status', 'contract_supplier', 'contract_start_date', 'contract_end_date', 'contract_renewal_date', 'contract_days_until_expiry', 'contract_cost', 'contract_billing_cycle', 'contract_auto_renew', 'contract_covered_assets', 'contract_sla_response_time', 'contract_sla_resolution_time', 'contract_coverage_hours'],
          'warranty_expiration': ['warranty_asset', 'warranty_type', 'warranty_supplier', 'warranty_start_date', 'warranty_end_date', 'warranty_days_remaining', 'warranty_status', 'warranty_cost', 'warranty_reference'],
          'asset_disposal_eol': ['disposal_asset', 'disposal_status', 'disposal_cancelled_at', 'disposal_cancelled_by', 'disposal_cancellation_reason', 'disposal_date', 'disposal_method', 'disposal_sanitization_method', 'disposal_sanitization_certificate', 'disposal_sanitized_by', 'disposal_recipient', 'disposal_proceeds', 'disposal_weee_compliant', 'disposal_notes'],
          'hardware_inventory': ['hw_item_type', 'hw_name', 'hw_manufacturer', 'hw_category', 'hw_part_number', 'hw_total_stock', 'hw_available', 'hw_min_qty', 'hw_status'],
          'custody_compliance': ['custody_asset', 'custody_holder', 'custody_status', 'custody_accepted_date', 'custody_eula_version', 'custody_signature_provider', 'custody_qms_reference', 'custody_ip_address', 'custody_created_date']
        };

        function renderVisualColumns() {
          activeList.innerHTML = '';
          availableList.innerHTML = '';

          const selectedType = reportTypeSelect ? reportTypeSelect.value : 'asset_summary';
          const validCols = columnsByReportType[selectedType] || [];

          const currentChecks = Array.from(colsContainer!.querySelectorAll('.form-check')) as HTMLElement[];
          let activeCount = 0;

          currentChecks.forEach((checkDiv) => {
            const input = checkDiv.querySelector('input') as HTMLInputElement | null;
            const label = checkDiv.querySelector('label') as HTMLElement | null;
            if (!input) return;

            const val = input.value;
            if (!validCols.includes(val)) {
              input.checked = false;
              return;
            }

            const isChecked = input.checked;
            const labelText = label ? label.textContent || val : val;

            if (isChecked) {
              activeCount++;
              const activeBadge = document.createElement('div');
              activeBadge.className = 'report-designer-active-badge d-flex align-items-center justify-content-between p-2 bg-primary-lt rounded-2 border border-primary-subtle';
              activeBadge.innerHTML = `
                  <div class="d-flex align-items-center">
                      <span class="report-designer-active-index badge bg-primary text-primary-fg me-2">${activeCount}</span>
                      <span class="small fw-semibold text-primary">${escapeHtml(labelText.trim())}</span>
                  </div>
                  <div class="d-flex align-items-center gap-1">
                      <button type="button" class="btn btn-sm btn-icon btn-outline-primary py-0 px-1 border-0 btn-move-up" title="${gettext('Move Up')}">
                          <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="14" height="14" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M12 5l0 14" /><path d="M18 11l-6 -6" /><path d="M6 11l6 -6" /></svg>
                      </button>
                      <button type="button" class="btn btn-sm btn-icon btn-outline-primary py-0 px-1 border-0 btn-move-down" title="${gettext('Move Down')}">
                          <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="14" height="14" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M12 5l0 14" /><path d="M18 13l-6 6" /><path d="M6 13l6 6" /></svg>
                      </button>
                      <button type="button" class="btn btn-sm btn-icon btn-outline-danger py-0 px-1 border-0 btn-remove-col" title="${gettext('Remove')}">
                          <svg xmlns="http://www.w3.org/2000/svg" class="icon" width="14" height="14" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round"><path stroke="none" d="M0 0h24v24H0z" fill="none"/><path d="M18 6l-12 12" /><path d="M6 6l12 12" /></svg>
                      </button>
                  </div>
              `;

              activeBadge.querySelector('.btn-remove-col')!.addEventListener('click', function() {
                input.checked = false;
                colsContainer!.appendChild(checkDiv);
                renderVisualColumns();
              });

              activeBadge.querySelector('.btn-move-up')!.addEventListener('click', function() {
                const prev = checkDiv.previousElementSibling;
                if (prev && prev.classList.contains('form-check')) {
                  colsContainer!.insertBefore(checkDiv, prev);
                  renderVisualColumns();
                }
              });

              activeBadge.querySelector('.btn-move-down')!.addEventListener('click', function() {
                const next = checkDiv.nextElementSibling;
                if (next && next.classList.contains('form-check')) {
                  colsContainer!.insertBefore(checkDiv, next.nextSibling);
                  renderVisualColumns();
                }
              });

              activeList.appendChild(activeBadge);
            } else {
              const availPill = document.createElement('button');
              availPill.type = 'button';
              availPill.className = 'report-designer-available-pill btn btn-sm btn-outline-secondary d-flex align-items-center py-1 px-2 text-start';
              availPill.innerHTML = `
                  <i class="mdi mdi-plus me-1"></i>
                  ${escapeHtml(labelText.trim())}
              `;

              availPill.addEventListener('click', function() {
                input.checked = true;
                colsContainer!.appendChild(checkDiv);
                renderVisualColumns();
              });

              availableList.appendChild(availPill);
            }
          });

          if (activeCount === 0) {
            activeList.innerHTML = '<span class="text-muted small text-center my-auto py-3 italic">' + gettext('No columns selected. Click available columns to add them.') + '</span>';
          }
        }

        renderVisualColumns();

        if (reportTypeSelect) {
          reportTypeSelect.addEventListener('change', function() {
            const selectedType = reportTypeSelect.value;
            const validCols = columnsByReportType[selectedType] || [];
            const checkboxes = colsContainer!.querySelectorAll('input[name="included_columns"]') as NodeListOf<HTMLInputElement>;
            checkboxes.forEach(cb => {
              if (!validCols.includes(cb.value)) {
                cb.checked = false;
              }
            });
            renderVisualColumns();
          });
        }
      }
    }

    // Live Preview Button Setup
    const submitBtn = reportEditor.querySelector('input[name="submit"], button[type="submit"]');
    const existingPreviewBtn = document.getElementById('btn-preview-report');

    if (submitBtn && !existingPreviewBtn) {
      const previewBtn = document.createElement('button');
      previewBtn.type = 'button';
      previewBtn.id = 'btn-preview-report';
      previewBtn.className = 'report-preview-trigger btn btn-outline-info ms-2';
      previewBtn.innerHTML = '<i class="mdi mdi-eye-outline me-1"></i> ' + gettext('Preview Report');

      submitBtn.parentNode!.insertBefore(previewBtn, submitBtn.nextSibling);

      previewBtn.addEventListener('click', function(e) {
        e.preventDefault();
        openReportPreviewModal();
      });
    }
  }

  function openReportPreviewModal() {
    const spinner = document.getElementById('previewSpinner');
    const frame = document.getElementById('previewFrame') as HTMLIFrameElement | null;

    if (spinner) {
      spinner.classList.remove('d-none');
      spinner.classList.add('d-flex');
    }
    if (frame) {
      frame.classList.add('d-none');
      frame.srcdoc = '';
    }

    // Initialize and trigger Bootstrap Modal
    const modalEl = document.getElementById('previewModal');
    if (modalEl) {
      const modalInstance = bootstrap.Modal.getOrCreateInstance(modalEl);
      modalInstance.show();
    }

    // Gather form data
    const formEl = document.querySelector('#report-template-editor form') as HTMLFormElement | null;
    if (!formEl) return;
    const formData = new FormData(formEl);

    // Fetch rendered HTML from view. The URL comes from the modal's
    // data-preview-url (Django {% url %}) so it survives URL-prefix changes
    // (the hardcoded '/reports/templates/preview/' was missing the 'extras/'
    // mount prefix -> 404). Fall back to the canonical path.
    const previewUrl = (modalEl && modalEl.getAttribute('data-preview-url')) || '/extras/reports/templates/preview/';
    const cspNonce = document.querySelector('meta[name="csp-nonce"]')?.getAttribute('content');
    fetch(previewUrl, {
      method: 'POST',
      body: formData,
      headers: {
        'X-Requested-With': 'XMLHttpRequest',
        ...(cspNonce ? { 'HX-Request': 'true', 'X-CSP-Nonce': cspNonce } : {})
      }
    })
    .then(response => {
      if (!response.ok) {
        return response.text().then(text => {
          throw new Error(text || gettext('Template rendering failed.'));
        });
      }
      return response.text();
    })
    .then(html => {
      if (spinner) {
        spinner.classList.add('d-none');
        spinner.classList.remove('d-flex');
      }
      if (frame) {
        frame.classList.remove('d-none');
        frame.srcdoc = html;
      }
    })
    .catch(error => {
      console.error('Error generating preview:', error);
      if (spinner) {
        spinner.classList.add('d-none');
        spinner.classList.remove('d-flex');
      }
      if (frame) {
        frame.classList.remove('d-none');
        const cleanErr = String(error instanceof Error ? error.message : error);
        frame.srcdoc = `<div class="report-preview-error">
            <h4>${gettext('Preview Render Error')}</h4>
            <pre>${escapeHtml(cleanErr)}</pre>
        </div>`;
      }
    });
  }

  function initSwappedReportTemplateForm(event: Event) {
    const target = (event as CustomEvent<{ target?: ParentNode }>).detail?.target;
    if (target) initReportTemplateForm(target);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initReportTemplateForm());
  } else {
    initReportTemplateForm();
  }
  document.addEventListener('htmx:afterSwap', initSwappedReportTemplateForm);
})();
