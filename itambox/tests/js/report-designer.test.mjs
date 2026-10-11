import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { transformSync } from 'esbuild';

const itamboxRoot = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const source = readFileSync(resolve(itamboxRoot, 'static/src/report-designer.ts'), 'utf8');
const compiled = transformSync(source, {
  loader: 'ts',
  format: 'iife',
  target: 'es2020',
}).code;

// Read one report type's selectable column list straight from the designer
// source; the lists mirror the server-side published column keys.
function columnListFor(reportType) {
  const match = new RegExp(`'${reportType}':\\s*\\[([\\s\\S]*?)\\]`).exec(source);
  assert.ok(match, `columnsByReportType must declare '${reportType}'`);
  return [...match[1].matchAll(/'([^']+)'/g)].map((entry) => entry[1]);
}

class FakeNode {
  constructor(tagName, id = '') {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.nextSibling = null;
    this.listeners = new Map();
    this.listenerCounts = new Map();
    this.queryResults = new Map();
    this.type = '';
    this.className = '';
    this.innerHTML = '';
    this.srcdoc = '';
    this.attributes = new Map();
    this.classList = {
      add: (name) => {
        const classes = this.className.split(/\s+/).filter(Boolean);
        if (!classes.includes(name)) classes.push(name);
        this.className = classes.join(' ');
      },
      remove: (name) => {
        this.className = this.className.split(/\s+/).filter((item) => item && item !== name).join(' ');
      },
    };
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
    this.listenerCounts.set(name, (this.listenerCounts.get(name) || 0) + 1);
  }

  querySelector(selector) {
    return this.queryResults.get(selector) || null;
  }

  querySelectorAll(selector) {
    const results = this.queryResults.get(selector);
    return (typeof results === 'function' ? results() : results) || [];
  }

  getAttribute(name) {
    return this.attributes.get(name) || null;
  }

  insertBefore(node, referenceNode) {
    if (node === referenceNode) return node;
    if (node.parentNode) {
      const previousIndex = node.parentNode.children.indexOf(node);
      if (previousIndex >= 0) node.parentNode.children.splice(previousIndex, 1);
    }
    const index = referenceNode ? this.children.indexOf(referenceNode) : -1;
    if (index < 0) this.children.push(node);
    else this.children.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }

  appendChild(node) {
    return this.insertBefore(node, null);
  }
}

class FakeDocument {
  constructor({ submitButton, reportEditor, reportModal, reportTypeSelect = null, elements = new Map() }) {
    this.readyState = 'complete';
    this.submitButton = submitButton;
    this.reportEditor = reportEditor;
    this.reportModal = reportModal;
    this.reportTypeSelect = reportTypeSelect;
    this.elements = elements;
    this.listeners = new Map();
    this.globalQueries = [];
  }

  getElementById(id) {
    if (this.elements.has(id)) return this.elements.get(id);
    if (id === 'report-template-editor') return this.reportEditor;
    if (id === 'previewModal') return this.reportModal;
    return null;
  }

  querySelector(selector) {
    this.globalQueries.push(selector);
    if (selector === '#report-template-editor') return this.reportEditor;
    if (selector === 'select[name="report_type"]') return this.reportTypeSelect;
    if (selector === 'input[name="submit"]' || selector === 'button[type="submit"]' || selector === '.btn-primary') {
      return this.submitButton;
    }
    return null;
  }

  createElement(tagName) {
    return new FakeNode(tagName);
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }
}

function makeReportEditorFixture(savedSequence) {
  const submitButton = new FakeNode('button', 'report-submit');
  submitButton.type = 'submit';
  const submitParent = new FakeNode('div', 'report-actions');
  submitParent.appendChild(submitButton);
  const reportEditor = new FakeNode('div', 'report-template-editor');
  reportEditor.queryResults.set('input[name="submit"], button[type="submit"]', submitButton);
  const reportModal = new FakeNode('div', 'previewModal');
  const reportTypeSelect = new FakeNode('select', 'report-type');
  reportTypeSelect.value = 'asset_summary';
  const activeList = new FakeNode('div', 'active-cols-list');
  const availableList = new FakeNode('div', 'available-cols-list');
  const managerWrapper = new FakeNode('div', 'visual-cols-manager-wrapper');
  managerWrapper.queryResults.set('#active-cols-list', activeList);
  managerWrapper.queryResults.set('#available-cols-list', availableList);

  const checkboxes = ['name', 'asset_tag'].map((value) => {
    const check = new FakeNode('div');
    check.className = 'form-check';
    const input = { value, checked: false };
    check.queryResults.set('input', input);
    check.queryResults.set('label', { textContent: value });
    return { check, input };
  });
  const columnsContainer = new FakeNode('div', 'div_id_included_columns');
  checkboxes.forEach(({ check }) => columnsContainer.appendChild(check));
  columnsContainer.appendChild(managerWrapper);
  columnsContainer.queryResults.set('.form-check', () =>
    columnsContainer.children.filter((child) => child.className.split(/\s+/).includes('form-check')),
  );
  columnsContainer.queryResults.set('input[name="included_columns"]', () =>
    columnsContainer.querySelectorAll('.form-check').map((check) => check.querySelector('input')),
  );

  const elements = new Map([
    ['report-template-editor', reportEditor],
    ['previewModal', reportModal],
    ['div_id_included_columns', columnsContainer],
    ['report-template-saved-sequence', { textContent: JSON.stringify(savedSequence) }],
    ['visual-cols-manager-wrapper', managerWrapper],
    ['btn-preview-report', new FakeNode('button', 'btn-preview-report')],
  ]);
  return { submitButton, submitParent, reportEditor, reportModal, reportTypeSelect, columnsContainer, checkboxes, elements };
}

function columnOrder(columnsContainer) {
  return columnsContainer.querySelectorAll('.form-check').map((check) => check.querySelector('input').value);
}

function runDesigner({ page }) {
  const submitButton = new FakeNode('button', `${page}-submit`);
  submitButton.type = 'submit';
  const submitParent = new FakeNode('div', `${page}-actions`);
  submitParent.insertBefore(submitButton, null);

  const reportEditor = page === 'report' ? new FakeNode('div', 'report-template-editor') : null;
  if (reportEditor) {
    reportEditor.queryResults.set('input[name="submit"], button[type="submit"]', submitButton);
  }
  const reportModal = page === 'report' ? new FakeNode('div', 'previewModal') : null;
  const document = new FakeDocument({ submitButton, reportEditor, reportModal });
  const context = createContext({
    console,
    document,
    gettext: (message) => message,
  });

  runInContext(compiled, context);
  return { document, submitParent };
}

test('notification swaps preserve editor state while a replacement editor initializes once', () => {
  const editor = makeReportEditorFixture(['asset_tag', 'name']);
  const document = new FakeDocument(editor);
  const context = createContext({
    console,
    document,
    gettext: (message) => message,
  });
  runInContext(compiled, context);

  assert.deepEqual(columnOrder(editor.columnsContainer), ['asset_tag', 'name']);
  assert.equal(editor.reportTypeSelect.listenerCounts.get('change'), 1);

  const [nameCheck, assetTagCheck] = editor.checkboxes;
  editor.columnsContainer.insertBefore(nameCheck.check, assetTagCheck.check);
  const notificationTarget = new FakeNode('div', 'notification-dropdown-content');
  const afterSwap = document.listeners.get('htmx:afterSwap');
  for (let index = 0; index < 3; index++) {
    afterSwap({ detail: { target: notificationTarget } });
  }

  assert.deepEqual(columnOrder(editor.columnsContainer), ['name', 'asset_tag'], 'notification swaps retain unsaved ordering');
  assert.equal(editor.reportTypeSelect.listenerCounts.get('change'), 1, 'the existing editor keeps one report-type listener');

  const replacement = makeReportEditorFixture(['name', 'asset_tag']);
  replacement.elements.delete('btn-preview-report');
  document.reportEditor = replacement.reportEditor;
  document.reportModal = replacement.reportModal;
  document.reportTypeSelect = replacement.reportTypeSelect;
  document.elements = replacement.elements;
  afterSwap({ detail: { target: replacement.reportEditor } });
  afterSwap({ detail: { target: replacement.reportEditor } });

  assert.equal(replacement.reportTypeSelect.listenerCounts.get('change'), 1, 'the replacement editor initializes its listener only once');
  assert.equal(replacement.submitParent.children[1].id, 'btn-preview-report', 'the replacement editor gets its preview control');
});

test('report preview is not injected into an unrelated login submit form', () => {
  const { submitParent, document } = runDesigner({ page: 'login' });

  assert.equal(submitParent.children.length, 1, 'login keeps only its original submit button');
  assert.equal(submitParent.children[0].id, 'login-submit');
  assert.deepEqual(document.globalQueries, [], 'login must not be queried as a report form');
});

test('report preview remains available inside the report template editor', () => {
  const { submitParent } = runDesigner({ page: 'report' });

  assert.equal(submitParent.children.length, 2, 'report editor receives one preview button');
  assert.equal(submitParent.children[1].id, 'btn-preview-report');
});

test('report preview escapes error text before rendering it in the iframe', async () => {
  const submitButton = new FakeNode('button', 'report-submit');
  const submitParent = new FakeNode('div', 'report-actions');
  submitParent.insertBefore(submitButton, null);
  const reportEditor = new FakeNode('div', 'report-template-editor');
  reportEditor.queryResults.set('input[name="submit"], button[type="submit"]', submitButton);
  const reportModal = new FakeNode('div', 'previewModal');
  reportModal.attributes.set('data-preview-url', '/extras/reports/templates/preview/');
  const spinner = new FakeNode('div', 'previewSpinner');
  const frame = new FakeNode('iframe', 'previewFrame');
  const form = {};
  const document = {
    readyState: 'complete',
    getElementById(id) {
      return new Map([
        ['report-template-editor', reportEditor],
        ['previewModal', reportModal],
        ['div_id_included_columns', null],
        ['btn-preview-report', null],
        ['previewSpinner', spinner],
        ['previewFrame', frame],
      ]).get(id) || null;
    },
    querySelector(selector) {
      if (selector === '#report-template-editor form') return form;
      if (selector === 'meta[name="csp-nonce"]') return null;
      return null;
    },
    createElement(tagName) {
      return new FakeNode(tagName);
    },
    addEventListener() {},
  };
  const errorPayload = '<img src=x onerror=alert(1)>';
  const context = createContext({
    console: { error() {} },
    document,
    gettext: (message) => message,
    FormData: class {
      constructor() {}
    },
    bootstrap: { Modal: { getOrCreateInstance: () => ({ show() {} }) } },
    fetch: async () => ({ ok: false, text: async () => errorPayload }),
  });

  runInContext(compiled, context);
  submitParent.children[1].listeners.get('click')({ preventDefault() {} });
  await new Promise((resolve) => setImmediate(resolve));

  assert.ok(frame.srcdoc.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!frame.srcdoc.includes(errorPayload));
});

test('report designer offers the agreement entitlement without license seat keys', () => {
  const subscriptionColumns = columnListFor('subscription_renewals');

  assert.ok(
    subscriptionColumns.includes('agreement_entitled_quantity'),
    'subscription columns must expose the agreement entitlement',
  );
  for (const seatKey of ['seats', 'assigned_seats', 'available_seats']) {
    assert.ok(!subscriptionColumns.includes(seatKey), `${seatKey} belongs to the license utilization report`);
  }
});

test('report designer omits the removed warranty provider column', () => {
  const warrantyColumns = columnListFor('warranty_expiration');

  assert.ok(warrantyColumns.includes('warranty_supplier'), 'warranty columns must expose the supplier');
  assert.ok(!warrantyColumns.includes('warranty_provider'), 'the removed provider column must not stay selectable');
});
