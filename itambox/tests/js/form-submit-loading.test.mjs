import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { transformSync } from 'esbuild';

const source = transformSync(readFileSync(new URL('../../static/src/form-submit-loading.ts', import.meta.url), 'utf8'), { loader: 'ts' }).code;

function harness() {
  const listeners = new Map();
  const body = { dataset: {}, addEventListener(name, fn) { const rows = listeners.get(name) || []; rows.push(fn); listeners.set(name, rows); } };
  const button = { innerHTML: 'Download export', textContent: 'Download export', classList: { add() {}, remove() {} }, replaceChildren() {} };
  const form = { tagName: 'FORM', dataset: {}, querySelector: () => button };
  const context = vm.createContext({ document: { body, createTextNode: text => text, createElement: () => ({ setAttribute() {} }) }, HTMLInputElement: class {}, gettext: text => text });
  return {
    body,
    form,
    load: () => vm.runInContext(source, context),
    submit() {
      const event = {
        target: form,
        submitter: button,
        defaultPrevented: false,
        propagationStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.propagationStopped = true; },
      };
      for (const fn of listeners.get('submit') || []) fn(event);
      return event;
    },
  };
}

test('boosted page script re-evaluation does not cancel the first native submission', () => {
  const page = harness();
  page.load();
  page.load();
  assert.equal(page.submit().defaultPrevented, false);
  const repeatedSubmit = page.submit();
  assert.equal(repeatedSubmit.defaultPrevented, true, 'an actual second submit still cannot duplicate a write');
  assert.equal(repeatedSubmit.propagationStopped, true);
});

test('download forms remain usable after their native attachment response', () => {
  const page = harness();
  page.form.dataset.submitLoading = 'false';
  page.load();
  assert.equal(page.submit().defaultPrevented, false);
  assert.equal(page.form.dataset.submitting, undefined);
  assert.equal(page.submit().defaultPrevented, false);
});
