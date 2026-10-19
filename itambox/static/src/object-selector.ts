/**
 * ITAMbox — Tom Select integration for FK/M2M fields.
 *
 * Auto-initializes Tom Select on any <select> element with the
 * data-tom-select attribute, providing searchable dropdowns.
 *
 * Usage in templates:
 *   <select name="manufacturer_id" data-tom-select>
 *     <option value="">---------</option>
 *     {% for obj in objects %}<option value="{{ obj.pk }}">{{ obj }}</option>{% endfor %}
 *   </select>
 *
 * For dynamically loaded options via HTMX, use:
 *   data-tom-select-url="{% url 'api:...' %}"
 *   data-tom-select-value-field="id"
 *   data-tom-select-label-field="name"
 */
(function () {
  interface TomSelectLoadResponse {
    results?: unknown[];
  }

  function initTomSelect(el: HTMLSelectElement): TomSelect | null {
    const originalLabel = el.id
      ? document.querySelector<HTMLLabelElement>(`label[for="${CSS.escape(el.id)}"]`)
      : null;
    const labelText = originalLabel?.textContent?.trim();
    if (labelText && !el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby')) {
      el.setAttribute('aria-label', labelText);
    }
    const url = el.getAttribute('data-tom-select-url');
    const plugins = ['dropdown_input'];
    if (el.multiple) {
      plugins.push('remove_button');
    } else {
      plugins.push('clear_button');
    }

    const options: TomSelectOptions = {
      plugins: plugins,
      create: false,
      allowEmptyOption: true,
      preload: 'focus',
      sortField: [
        { field: '$score', direction: 'desc' },
        { field: '$order' }
      ],
      render: {
        no_results: function () {
          return '<div class="no-results">' + gettext('No results found') + '</div>';
        } as unknown as () => { wrapper: string },
      },
    };

    if (url) {
      options.valueField = el.getAttribute('data-tom-select-value-field') || 'id';
      options.labelField = el.getAttribute('data-tom-select-label-field') || 'name';
      options.searchField = options.labelField as string;
      options.load = function (query: string, callback: (results?: unknown[]) => void) {
        const headers: Record<string, string> = { Accept: 'application/json' };
        const token = ITAMboxState ? ITAMboxState.getCSRFToken() : '';
        if (token) headers['X-CSRFToken'] = token;
        fetch(url + '?q=' + encodeURIComponent(query), { headers, credentials: 'same-origin' })
          .then(function (response) {
            return response.json();
          })
          .then(function (json: TomSelectLoadResponse) {
            callback(json.results || (json as unknown as unknown[]));
          })
          .catch(function () {
            callback();
          });
      };
    }

    const ts = new TomSelect(el, options) as any;

    // Keep the document's empty option on top of the list.
    //
    // Tom Select numbers every option with `$order` and sorts by that field as
    // long as the query is empty. Options parsed from the document keep the
    // parser's number (the empty option is 1), but options delivered by `load`
    // take `$order = ++order` from an instance counter that starts at 0 — so
    // the first loaded option is `$order: 1` as well. Sifter breaks that tie by
    // object key order, which lists integer keys before the empty key, and a
    // real option ends up above "---------". Continuing the counter after the
    // document's options removes the tie, so the empty entry stays first (it
    // also keeps the generated option ids unique).
    if (ts && url) {
      let documentOrder = 0;
      const currentOptions = ts.options || {};
      for (const key of Object.keys(currentOptions)) {
        const order = Number(currentOptions[key]?.$order);
        if (Number.isFinite(order) && order > documentOrder) {
          documentOrder = order;
        }
      }
      if (documentOrder > ts.order) {
        ts.order = documentOrder;
      }
    }

    if (ts && ts.control_input && el.id) {
      const inputId = el.id + '-ts-input';
      ts.control_input.id = inputId;
      if (el.name) {
        ts.control_input.setAttribute('name', el.name + '-ts-control');
      }
      const label = originalLabel
        || document.getElementById(el.id + '-ts-label')
        || document.querySelector('label[for="' + el.id + '-ts-control"]');
      if (label) {
        label.setAttribute('for', inputId);
      }
    }
    return ts;
  }

  function initAll(): void {
    document.querySelectorAll<HTMLSelectElement>('select[data-tom-select]').forEach(function (sel: HTMLSelectElement) {
      if ((sel as unknown as Record<string, unknown>).tomselect) return;
      initTomSelect(sel);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }

  // Re-init after HTMX content swaps
  document.body.addEventListener('htmx:afterSettle', function () {
    initAll();
  });

  // Clean up Tom Select instances before HTMX removes their <select> from the DOM.
  // htmx fires `htmx:beforeCleanupElement` on EACH element it cleans up (recursing into
  // children), with the element exposed as `detail.elt`. TomSelect registers listeners on
  // document/window; unless we call destroy() they orphan on every boosted swap and pile up
  // without bound (measured: ~12 leaked document listeners per asset-list visit).
  // NOTE: htmx has no `htmx:beforeCleanUp` event and the detail carries `elt`, not `target`
  // — the previous handler was bound to a non-existent event AND read the wrong property,
  // so it never ran.
  document.body.addEventListener('htmx:beforeCleanupElement', function (evt: Event) {
    const el = (evt as CustomEvent).detail.elt as HTMLElement | undefined;
    if (!el || typeof el.matches !== 'function' || !el.matches('select[data-tom-select]')) return;
    const ts = (el as any).tomselect;
    if (ts && typeof ts.destroy === 'function') {
      try {
        ts.destroy();
      } catch (_e) {
        // Ignore
      }
    }
  });
})();
