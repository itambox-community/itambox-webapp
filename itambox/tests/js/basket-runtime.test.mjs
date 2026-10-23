import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import test from 'node:test';

const itamboxRoot = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const fakeTimers = new Map();
let nextTimerId = 1;

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) {
    names.forEach((name) => this.values.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.values.delete(name));
  }

  toggle(name, force) {
    const shouldAdd = force === undefined ? !this.values.has(name) : force;
    if (shouldAdd) this.values.add(name);
    else this.values.delete(name);
    return shouldAdd;
  }

  contains(name) {
    return this.values.has(name);
  }
}

class FakeElement {
  constructor(id, tagName = id) {
    this.id = id;
    this.tagName = String(tagName).toUpperCase();
    this.classList = new FakeClassList();
    this.dataset = {};
    this.listeners = new Map();
    this.children = [];
    this.attributes = new Map();
    this.textContent = '';
    this.value = '';
    this.type = '';
    this.name = '';
    this.placeholder = '';
    this.hidden = false;
    this.disabled = false;
    this.parentNode = null;
    this._className = '';
    this._innerHTML = '';
  }

  set className(value) {
    this._className = value;
    this.classList.values = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  get className() {
    return this._className;
  }

  set innerHTML(value) {
    this._innerHTML = value;
    if (value === '') this.children = [];
  }

  get innerHTML() {
    return this._innerHTML;
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  insertBefore(child, reference) {
    const index = this.children.indexOf(reference);
    if (index === -1) return this.appendChild(child);
    this.children.splice(index, 0, child);
    child.parentNode = this;
    return child;
  }

  removeChild(child) {
    this.children = this.children.filter((candidate) => candidate !== child);
  }

  remove() {
    this.parentNode?.removeChild(this);
  }

  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));

    const tagAndClass = selector.match(/^([a-z]+)\.([\w-]+)$/i);
    if (tagAndClass) {
      return this.tagName === tagAndClass[1].toUpperCase() && this.classList.contains(tagAndClass[2]);
    }

    const attribute = selector.match(/^([a-z]+)?\[(name|type|data-field|data-pk)="([^"]+)"\]$/i);
    if (attribute) {
      const [, tag, name, value] = attribute;
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      if (name === 'data-field') return this.dataset.field === value;
      if (name === 'data-pk') return this.dataset.pk === value;
      return this[name] === value;
    }

    return this.tagName === selector.toUpperCase();
  }

  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const match = child.querySelector(selector);
      if (match) return match;
    }
    return null;
  }

  querySelectorAll(selector) {
    const matches = [];
    for (const child of this.children) {
      if (child.matches(selector)) matches.push(child);
      matches.push(...child.querySelectorAll(selector));
    }
    return matches;
  }

  closest(selector) {
    let candidate = this;
    while (candidate) {
      if (candidate.matches(selector)) return candidate;
      candidate = candidate.parentNode;
    }
    return null;
  }

  dispatchEvent(event) {
    this.listeners.get(event.type)?.(event);
    return true;
  }

  focus() {
    this.focused = true;
  }
}

class FakeDocument {
  constructor(ids) {
    this.elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
    this.listeners = new Map();
    this.body = new FakeElement('body');
    this.elements.set('body', this.body);
  }

  getElementById(id) {
    return this.elements.get(id) || this.body.querySelector(`#${id}`);
  }

  createElement(tagName) {
    return new FakeElement('', tagName);
  }

  querySelectorAll(selector) {
    return this.body.querySelectorAll(selector);
  }

  addEventListener(name, handler) {
    const handlers = this.listeners.get(name) || [];
    handlers.push(handler);
    this.listeners.set(name, handlers);
  }

  dispatchEvent(event) {
    for (const handler of this.listeners.get(event.type) || []) handler(event);
    return true;
  }
}

function makeTemplateClone(kind) {
  const tr = new FakeElement('', 'tr');
  tr.className = kind === 'audit' ? 'audit-basket-row' : 'scan-basket-row';

  const inputFields = new Set(['pk', 'proceeds']);
  const fields = [
    'pk',
    'asset_tag',
    'label',
    'status',
    'assigned_to',
    'book_value',
    'proceeds',
    'warning',
    'classification',
    'observed_location',
  ];
  for (const field of fields) {
    const element = new FakeElement('', inputFields.has(field) ? 'input' : 'span');
    element.dataset.field = field;
    if (field === 'pk') {
      element.type = 'hidden';
      element.name = 'pk';
    }
    if (field === 'warning') element.hidden = true;
    tr.appendChild(element);
  }

  const remove = new FakeElement('', 'button');
  remove.className = kind === 'audit' ? 'audit-basket-remove' : 'scan-basket-remove';
  tr.appendChild(remove);
  return { querySelector: (selector) => tr.matches(selector) ? tr : tr.querySelector(selector) };
}

function makeDom(kind, options = {}) {
  const rootId = kind === 'audit' ? 'audit-basket-root' : 'scan-basket-root';
  const inputId = kind === 'audit' ? 'audit-basket-input' : 'scan-basket-input';
  const formId = kind === 'audit' ? 'audit-basket-form' : 'scan-basket-form';
  const rowsId = kind === 'audit' ? 'audit-basket-rows' : 'scan-basket-rows';
  const templateId = kind === 'audit' ? 'audit-basket-row-template' : 'scan-basket-row-template';
  const countId = kind === 'audit' ? 'audit-basket-count' : 'scan-basket-count';
  const openId = kind === 'audit' ? 'audit-open-scanner-btn' : 'basket-open-scanner-btn';
  const modalId = kind === 'audit' ? 'audit-scanner-modal' : 'basket-scanner-modal';
  const readerId = kind === 'audit' ? 'audit-scanner-reader' : 'basket-scanner-reader';
  const feedbackId = kind === 'audit' ? 'audit-scan-feedback' : 'basket-scan-feedback';
  const overlayCountId = kind === 'audit' ? 'audit-scanner-count' : 'basket-scanner-count';
  const closeId = kind === 'audit' ? 'audit-close-scanner-btn' : 'basket-close-scanner-btn';
  const torchId = kind === 'audit' ? 'audit-toggle-torch-btn' : 'basket-toggle-torch-btn';
  const errorId = kind === 'audit' ? 'audit-scanner-error' : 'basket-scanner-error';
  const ids = [
    rootId,
    inputId,
    formId,
    rowsId,
    templateId,
    countId,
    'scan-basket-empty',
    'scan-basket-clear',
    'scan-basket-submit',
    'scan-basket-dispose-confirm-text',
    'audit-basket-empty',
    'audit-basket-clear',
    'audit-basket-submit',
    openId,
    modalId,
    readerId,
    feedbackId,
    overlayCountId,
    closeId,
    torchId,
    errorId,
    'django-messages',
    'scan-seed-data',
  ];
  const document = new FakeDocument(ids);
  const root = document.getElementById(rootId);
  root.dataset.validateUrl = '/validate';
  root.dataset.resolveUrl = '/resolve';
  root.dataset.mode = options.mode || 'checkin';
  document.getElementById(templateId).content = {
    cloneNode: () => makeTemplateClone(kind),
  };
  const form = document.getElementById(formId);
  form.submitCount = 0;
  form.submit = () => { form.submitCount += 1; };

  let tenantField = null;
  const tenantType = options.tenantType ?? (kind === 'bulk' ? 'select' : null);
  if (tenantType) {
    tenantField = new FakeElement('tenant-field', tenantType === 'select' ? 'select' : 'input');
    tenantField.type = tenantType;
    tenantField.name = 'tenant';
    tenantField.value = options.tenantValue || '';
    tenantField.tomselect = options.tomSelect;
    document.elements.set(tenantField.id, tenantField);
    form.appendChild(tenantField);
  }

  root.appendChild(document.getElementById(inputId));
  root.appendChild(form);
  form.appendChild(document.getElementById(rowsId));
  document.body.appendChild(root);

  const seed = document.getElementById('scan-seed-data');
  seed.textContent = options.seeds ? JSON.stringify(options.seeds) : '';

  return {
    document,
    tenantField,
    ids: {
      rootId,
      inputId,
      formId,
      rowsId,
      countId,
      modalId,
      openId,
      readerId,
      feedbackId,
      overlayCountId,
      closeId,
    },
  };
}

function installGlobals(dom, readers, fetchImpl) {
  const schedule = (callback) => {
    const id = nextTimerId++;
    fakeTimers.set(id, callback);
    return id;
  };
  globalThis.document = dom.document;
  globalThis.window = {
    isSecureContext: true,
    setTimeout: schedule,
    clearTimeout: (id) => fakeTimers.delete(id),
  };
  globalThis.getComputedStyle = (element) => ({
    display: element.classList.contains('is-open') ? 'block' : 'none',
  });
  globalThis.setTimeout = schedule;
  globalThis.clearTimeout = (id) => fakeTimers.delete(id);
  globalThis.gettext = (message) => message;
  globalThis.ngettext = (singular, plural, count) => (count === 1 ? singular : plural);
  globalThis.interpolate = (message, context) => message.replace(/%\(([^)]+)\)s/g, (_, key) => context[key]);
  globalThis.fetch = fetchImpl;
  globalThis.Html5Qrcode = class {
    constructor(readerId) {
      this.readerId = readerId;
      this.isScanning = false;
      this.cleared = false;
      readers.push(this);
    }

    start(_camera, _config, onSuccess) {
      this.onSuccess = onSuccess;
      this.isScanning = true;
      return Promise.resolve();
    }

    stop() {
      this.isScanning = false;
      return Promise.resolve();
    }

    clear() {
      this.cleared = true;
    }

    getRunningTrackCapabilities() {
      return {};
    }
  };
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolvePromise) => setImmediate(resolvePromise));
}

function bulkPayload(pk, tenantId, overrides = {}) {
  return {
    found: true,
    pk,
    tenant_id: tenantId,
    label: `Bulk asset ${pk}`,
    asset_tag: `BULK-${pk}`,
    serial: `SERIAL-${pk}`,
    status: 'deployed',
    assigned_to: '',
    book_value: null,
    eligible: true,
    warning: null,
    ...overrides,
  };
}

async function loadBulkDom(options = {}, fetchImpl = () => Promise.resolve({ ok: false, status: 404 })) {
  const dom = makeDom('bulk', options);
  const readers = [];
  installGlobals(dom, readers, fetchImpl);
  const bundleUrl = pathToFileURL(resolve(itamboxRoot, 'tests/js/.build/scan-basket.mjs')).href;
  await import(`${bundleUrl}?tenant-baskets=${Date.now()}-${Math.random()}`);
  dom.document.dispatchEvent({ type: 'DOMContentLoaded' });
  return { dom, readers };
}

function visiblePks(dom) {
  return dom.document.getElementById(dom.ids.rowsId).children.map((row) => Number(row.dataset.pk));
}

test('initial target selection keeps same-tenant seeded rows, counts, and pk inputs', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  try {
    const seeds = [bulkPayload(1, 11), bulkPayload(2, 11)];
    const { dom } = await loadBulkDom({ seeds });
    const rows = dom.document.getElementById(dom.ids.rowsId);
    const count = dom.document.getElementById(dom.ids.countId);

    assert.equal(dom.tenantField.value, '11');
    assert.equal(count.textContent, '2');
    assert.deepEqual(visiblePks(dom), [1, 2]);
    assert.deepEqual(rows.children.map((row) => row.dataset.tenantId), ['11', '11']);

    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });

    assert.equal(count.textContent, '2');
    assert.deepEqual(visiblePks(dom), [1, 2]);
    assert.deepEqual(rows.querySelectorAll('input[name="pk"]').map((input) => input.value), ['1', '2']);
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('dispose confirmation uses the localized singular and plural asset forms', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  try {
    const { dom } = await loadBulkDom({ mode: 'dispose', seeds: [bulkPayload(1, 11), bulkPayload(2, 11)] });
    const confirmation = dom.document.getElementById('scan-basket-dispose-confirm-text');
    assert.equal(confirmation.textContent, 'Dispose of 2 assets?');

    const rows = dom.document.getElementById(dom.ids.rowsId);
    const remove = rows.children[0].querySelector('.scan-basket-remove');
    rows.dispatchEvent({ type: 'click', target: remove });
    assert.equal(confirmation.textContent, 'Dispose of 1 asset?');
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('single-tenant seeds preselect and render without user interaction', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  try {
    const tomSelectValues = [];
    const { dom } = await loadBulkDom({
      seeds: [bulkPayload(7, 22), bulkPayload(8, 22)],
      tomSelect: { setValue: (value) => tomSelectValues.push(value) },
    });

    assert.equal(dom.tenantField.value, '22');
    assert.deepEqual(tomSelectValues, ['22']);
    assert.equal(dom.document.getElementById(dom.ids.countId).textContent, '2');
    assert.deepEqual(visiblePks(dom), [7, 8]);
    assert.equal(dom.document.getElementById('scan-basket-submit').disabled, false);
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('mixed-tenant seeds switch losslessly and report rows kept aside', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  try {
    const seeds = [bulkPayload(1, 11), bulkPayload(2, 11), bulkPayload(3, 22)];
    const { dom } = await loadBulkDom({ seeds });
    const root = dom.document.getElementById(dom.ids.rootId);
    const notice = root.querySelector('#scan-basket-kept-aside');

    assert.equal(dom.tenantField.value, '');
    assert.deepEqual(visiblePks(dom), []);
    assert.equal(notice.hidden, true);

    dom.tenantField.value = '11';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    assert.deepEqual(visiblePks(dom), [1, 2]);
    assert.equal(notice.hidden, false);
    assert.match(notice.textContent, /^1 asset from another tenant/);

    dom.tenantField.value = '22';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    assert.deepEqual(visiblePks(dom), [3]);
    assert.match(notice.textContent, /^2 assets from another tenant/);

    dom.tenantField.value = '11';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    assert.deepEqual(visiblePks(dom), [1, 2]);

    dom.document.getElementById('scan-basket-clear').listeners.get('click')();
    assert.deepEqual(visiblePks(dom), []);
    assert.equal(notice.hidden, false);
    dom.tenantField.value = '22';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    assert.deepEqual(visiblePks(dom), [3]);
    assert.equal(notice.hidden, true);
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('dispose proceeds survive tenant switches', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  try {
    const seeds = [bulkPayload(1, 11, { book_value: '250.00' }), bulkPayload(2, 22)];
    const { dom } = await loadBulkDom({ mode: 'dispose', seeds });

    dom.tenantField.value = '11';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    const firstProceeds = dom.document
      .getElementById(dom.ids.rowsId)
      .querySelector('input[data-field="proceeds"]');
    firstProceeds.value = '125.50';
    firstProceeds.dispatchEvent({ type: 'input', target: firstProceeds });

    dom.tenantField.value = '22';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    dom.tenantField.value = '11';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });

    const restoredProceeds = dom.document
      .getElementById(dom.ids.rowsId)
      .querySelector('input[data-field="proceeds"]');
    assert.equal(restoredProceeds.name, 'proceeds_1');
    assert.equal(restoredProceeds.value, '125.50');
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('concrete hidden-tenant scope scans immediately and never clears on change', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const pending = [];
  try {
    const { dom } = await loadBulkDom(
      { tenantType: 'hidden', tenantValue: '11' },
      (url) => new Promise((resolvePromise) => pending.push({ url, resolvePromise })),
    );
    const input = dom.document.getElementById(dom.ids.inputId);

    assert.equal(input.disabled, false);
    assert.equal(dom.tenantField.listeners.has('change'), false);
    input.value = 'CONCRETE-ASSET';
    input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
    assert.equal(pending.length, 1);
    assert.match(pending[0].url, /[?&]tenant=11/);

    pending[0].resolvePromise({
      ok: true,
      status: 200,
      json: () => Promise.resolve(bulkPayload(9, 11)),
    });
    await flush();
    assert.deepEqual(visiblePks(dom), [9]);

    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    assert.deepEqual(visiblePks(dom), [9]);
    assert.equal(dom.document.getElementById(dom.ids.countId).textContent, '1');
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('aggregate submit and scanner controls require a selected tenant and active rows', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const pending = [];
  try {
    const { dom } = await loadBulkDom(
      {},
      (url) => new Promise((resolvePromise) => pending.push({ url, resolvePromise })),
    );
    const input = dom.document.getElementById(dom.ids.inputId);
    const camera = dom.document.getElementById(dom.ids.openId);
    const submit = dom.document.getElementById('scan-basket-submit');
    const clear = dom.document.getElementById('scan-basket-clear');

    assert.equal(input.disabled, true);
    assert.equal(camera.disabled, true);
    assert.equal(submit.disabled, true);

    input.value = 'BLOCKED-WITHOUT-TENANT';
    input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
    assert.equal(pending.length, 0);

    dom.tenantField.value = '11';
    dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
    assert.equal(input.disabled, false);
    assert.equal(camera.disabled, false);
    assert.equal(submit.disabled, true);

    input.value = 'ACTIVE-TENANT-ASSET';
    input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
    pending[0].resolvePromise({
      ok: true,
      status: 200,
      json: () => Promise.resolve(bulkPayload(10, 11)),
    });
    await flush();
    assert.equal(submit.disabled, false);

    clear.listeners.get('click')();
    assert.equal(submit.disabled, true);
    assert.deepEqual(visiblePks(dom), []);
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('real bulk and audit entrypoints keep manual input ungated and camera feedback in the overlay', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const bundleNames = { audit: 'audit-basket.mjs', bulk: 'scan-basket.mjs' };

  try {
    for (const kind of ['audit', 'bulk']) {
      const dom = makeDom(kind);
      const readers = [];
      const pending = [];
      installGlobals(dom, readers, (url) => new Promise((resolvePromise) => pending.push({ url, resolvePromise })));
      const bundleUrl = pathToFileURL(resolve(itamboxRoot, `tests/js/.build/${bundleNames[kind]}`)).href;
      await import(`${bundleUrl}?runtime=${kind}-${Date.now()}`);
      dom.document.dispatchEvent({ type: 'DOMContentLoaded' });
      if (kind === 'bulk') {
        dom.tenantField.value = '11';
        dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
      }

      const input = dom.document.getElementById(dom.ids.inputId);
      const openButton = dom.document.getElementById(dom.ids.openId);
      const feedback = dom.document.getElementById(dom.ids.feedbackId);
      const messages = dom.document.getElementById('django-messages');

      assert.equal(typeof input.listeners.get('keydown'), 'function', `${kind} manual handler is wired`);
      assert.equal(typeof openButton.listeners.get('click'), 'function', `${kind} camera handler is wired`);

      input.value = 'MANUAL-ONE';
      input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
      input.value = 'USB-TWO';
      input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
      assert.equal(pending.length, 2, `${kind} manual/USB entries bypass the camera gate`);

      pending.splice(0).forEach(({ resolvePromise }) => resolvePromise({ ok: false, status: 404 }));
      await flush();
      assert.equal(messages.children.length, 2, `${kind} closed-overlay errors use visible fallback toasts`);

      messages.children = [];
      openButton.listeners.get('click')();
      await flush();
      const reader = readers.at(-1);
      assert.equal(typeof reader.onSuccess, 'function', `${kind} camera reader is wired`);
      reader.onSuccess('CAMERA-404');
      assert.equal(pending.length, 1, `${kind} camera detection starts one domain action`);
      pending.splice(0).forEach(({ resolvePromise }) => resolvePromise({ ok: false, status: 404 }));
      await flush();

      assert.match(feedback.textContent, /No asset matches: CAMERA-404/);
      assert.equal(messages.children.length, 0, `${kind} open-overlay feedback is not hidden below the overlay`);
    }
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('stale camera promise settlement cannot mutate the replacement bulk or audit basket', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const bundleNames = { audit: 'audit-basket.mjs', bulk: 'scan-basket.mjs' };
  const payloadFor = (kind, pk) => kind === 'audit'
    ? {
      found: true,
      pk,
      label: `Audit asset ${pk}`,
      asset_tag: `AUDIT-${pk}`,
      serial: `SERIAL-${pk}`,
      status: 'deployed',
      classification: 'matched',
      observed_location: '',
      eligible: true,
      warning: null,
    }
    : {
      found: true,
      pk,
      label: `Bulk asset ${pk}`,
      asset_tag: `BULK-${pk}`,
      serial: `SERIAL-${pk}`,
      status: 'deployed',
      assigned_to: '',
      book_value: null,
      tenant_id: 11,
      eligible: true,
      warning: null,
    };

  try {
    for (const kind of ['audit', 'bulk']) {
      const dom = makeDom(kind);
      const readers = [];
      const pending = [];
      installGlobals(dom, readers, (url) => new Promise((resolvePromise) => pending.push({ url, resolvePromise })));
      const bundleUrl = pathToFileURL(resolve(itamboxRoot, `tests/js/.build/${bundleNames[kind]}`)).href;
      await import(`${bundleUrl}?stale-session=${kind}-${Date.now()}`);
      dom.document.dispatchEvent({ type: 'DOMContentLoaded' });
      if (kind === 'bulk') {
        dom.tenantField.value = '11';
        dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
      }

      const openButton = dom.document.getElementById(dom.ids.openId);
      const closeButton = dom.document.getElementById(dom.ids.closeId);
      const count = dom.document.getElementById(dom.ids.overlayCountId);
      const feedback = dom.document.getElementById(dom.ids.feedbackId);

      openButton.listeners.get('click')();
      await flush();
      const staleReader = readers.at(-1);
      staleReader.onSuccess('STALE-CAMERA');
      assert.equal(pending.length, 1, `${kind} stale camera action is pending`);

      closeButton.listeners.get('click')();
      openButton.listeners.get('click')();
      await flush();
      const replacementReader = readers.at(-1);
      assert.notEqual(replacementReader, staleReader, `${kind} reopened scanner has a replacement reader`);
      replacementReader.onSuccess('LIVE-CAMERA');
      assert.equal(pending.length, 2, `${kind} replacement camera action is pending`);

      pending[0].resolvePromise({
        ok: true,
        status: 200,
        json: () => Promise.resolve(payloadFor(kind, 1)),
      });
      await flush();
      assert.equal(count.textContent, '0', `${kind} stale settlement must not add to the replacement basket`);
      assert.equal(feedback.textContent, '', `${kind} stale settlement must not write replacement feedback`);

      pending[1].resolvePromise({
        ok: true,
        status: 200,
        json: () => Promise.resolve(payloadFor(kind, 2)),
      });
      await flush();
      assert.equal(count.textContent, '1', `${kind} live settlement adds exactly one asset`);
      assert.match(feedback.textContent, /Added:/, `${kind} live settlement still reports success`);
    }
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});

test('ambiguous EAN scans report the ambiguity instead of a plain miss', async () => {
  const originalTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const bundleNames = { audit: 'audit-basket.mjs', bulk: 'scan-basket.mjs' };

  try {
    for (const kind of ['audit', 'bulk']) {
      const dom = makeDom(kind);
      const readers = [];
      const pending = [];
      installGlobals(dom, readers, (url) => new Promise((resolvePromise) => pending.push({ url, resolvePromise })));
      const bundleUrl = pathToFileURL(resolve(itamboxRoot, `tests/js/.build/${bundleNames[kind]}`)).href;
      await import(`${bundleUrl}?ambiguous=${kind}-${Date.now()}`);
      dom.document.dispatchEvent({ type: 'DOMContentLoaded' });
      if (kind === 'bulk') {
        dom.tenantField.value = '11';
        dom.tenantField.dispatchEvent({ type: 'change', target: dom.tenantField });
      }

      const input = dom.document.getElementById(dom.ids.inputId);
      const feedback = dom.document.getElementById(dom.ids.feedbackId);

      // The resolver reports an EAN mapping to several assets distinctly from a
      // genuine miss; both travel as 404 with a JSON body.
      input.value = 'EAN-AMBIGUOUS';
      input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
      assert.equal(pending.length, 1, `${kind} ambiguous scan starts one resolve`);
      pending.splice(0).forEach(({ resolvePromise }) => resolvePromise({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ found: false, ambiguous: true }),
      }));
      await flush();
      assert.match(feedback.textContent, /EAN-AMBIGUOUS/, `${kind} ambiguous feedback names the scanned code`);
      assert.match(
        feedback.textContent,
        /matches several assets\. Scan the asset's label instead\./,
        `${kind} ambiguous scan reports distinct guidance`,
      );
      assert.doesNotMatch(feedback.textContent, /No asset matches/, `${kind} ambiguous scan is not a plain miss`);

      input.value = 'TRULY-MISSING';
      input.listeners.get('keydown')({ key: 'Enter', preventDefault() {} });
      assert.equal(pending.length, 1, `${kind} miss scan starts one resolve`);
      pending.splice(0).forEach(({ resolvePromise }) => resolvePromise({
        ok: false,
        status: 404,
        json: () => Promise.resolve({ found: false }),
      }));
      await flush();
      assert.match(feedback.textContent, /No asset matches: TRULY-MISSING/, `${kind} genuine miss keeps the current message`);
    }
  } finally {
    globalThis.setTimeout = originalTimers.setTimeout;
    globalThis.clearTimeout = originalTimers.clearTimeout;
  }
});
