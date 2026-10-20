import assert from 'node:assert/strict';
import test from 'node:test';

class FakeClassList {
  #values = new Set();

  add(...names) {
    names.forEach((name) => this.#values.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.#values.delete(name));
  }

  contains(name) {
    return this.#values.has(name);
  }
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.classList = new FakeClassList();
    this.dataset = {};
    this.listeners = new Map();
    this.attributes = new Map();
    this.textContent = '';
    this.innerHTML = '';
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

  querySelector() {
    return null;
  }
}

const ids = [
  'reader',
  'modal',
  'torch',
  'open',
  'close',
  'audit-open-scanner-btn',
  'audit-scanner-reader',
  'audit-scanner-modal',
  'audit-toggle-torch-btn',
  'audit-close-scanner-btn',
  'audit-scanner-error',
  'barcode-scan-input',
  'global-scanner-reader',
  'global-scanner-modal',
  'global-toggle-torch-btn',
  'global-open-scanner-btn',
  'global-close-scanner-btn',
  'global-scanner-error',
  'global-scanner-feedback',
];
const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
const body = new FakeElement('body');

globalThis.document = {
  body,
  listeners: new Map(),
  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  },
  dispatchEvent() {},
  getElementById(id) {
    return elements.get(id) || null;
  },
};
globalThis.window = { isSecureContext: true, location: {}, setTimeout, clearTimeout };
globalThis.gettext = (message) => message;
globalThis.interpolate = (message, context) => message.replace('%(code)s', context.code);
globalThis.fetch = () => Promise.resolve({ ok: false });

const readers = [];
const stopResolvers = [];
const startResolvers = [];
let deferNextStart = false;

class FakeHtml5Qrcode {
  constructor(readerId) {
    this.readerId = readerId;
    this.isScanning = false;
    this.cleared = false;
    this.stopCalls = 0;
    readers.push(this);
    const readerElement = elements.get(readerId);
    if (readerElement) readerElement.innerHTML = `reader-${readers.length}`;
  }

  start(_camera, _config, onSuccess) {
    this.onSuccess = onSuccess;
    if (deferNextStart) {
      deferNextStart = false;
      return new Promise((resolve) => {
        startResolvers.push(() => {
          this.isScanning = true;
          resolve();
        });
      });
    }
    this.isScanning = true;
    return Promise.resolve();
  }

  getRunningTrackCapabilities() {
    return {};
  }

  stop() {
    this.stopCalls += 1;
    return new Promise((resolve) => {
      stopResolvers.push(() => {
        this.isScanning = false;
        resolve();
      });
    });
  }

  clear() {
    this.cleared = true;
    const readerElement = elements.get(this.readerId);
    if (readerElement) readerElement.innerHTML = '';
  }
}

globalThis.Html5Qrcode = FakeHtml5Qrcode;
const { AssetScanner } = await import('./.build/scanner.mjs');

function config(onResult = () => undefined) {
  return {
    readerId: 'reader',
    modalId: 'modal',
    torchId: 'torch',
    openBtnId: 'open',
    closeBtnId: 'close',
    onResult,
  };
}

test('a stale reader stop callback cannot clear a replacement scanner', async () => {
  readers.length = 0;
  stopResolvers.length = 0;
  const scanner = new AssetScanner(config());

  await scanner.start();
  scanner.stop();
  await scanner.start();
  assert.equal(readers.length, 2);
  assert.equal(stopResolvers.length, 1);

  // Resolve reader A after reader B has replaced the instance field.
  stopResolvers[0]();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(readers[0].stopCalls, 1, 'the stale reader is stopped');
  assert.equal(readers[0].cleared, false, 'stale cleanup must not clear shared replacement DOM');
  assert.equal(readers[1].cleared, false, 'the replacement reader remains active');
  assert.equal(elements.get('reader').innerHTML, 'reader-2', 'stale cleanup must not clear the replacement DOM');
});

test('a stale reader from another scanner instance cannot clear replacement DOM', async () => {
  readers.length = 0;
  stopResolvers.length = 0;
  const staleScanner = new AssetScanner(config());
  const replacementScanner = new AssetScanner(config());

  await staleScanner.start();
  staleScanner.stop();
  await replacementScanner.start();
  assert.equal(readers.length, 2);
  assert.equal(stopResolvers.length, 1);

  // The old instance settles after the independent replacement has claimed the
  // same reader ID. Ownership must therefore be shared across instances.
  stopResolvers[0]();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(readers[0].stopCalls, 1, 'the stale reader is stopped');
  assert.equal(readers[0].cleared, false, 'an old instance must not clear the replacement');
  assert.equal(readers[1].cleared, false, 'the replacement reader remains active');
  assert.equal(elements.get('reader').innerHTML, 'reader-2', 'cross-instance cleanup must preserve replacement DOM');
});

test('a stale reader callback from another scanner instance cannot reach its old handler', async () => {
  readers.length = 0;
  stopResolvers.length = 0;
  const staleResults = [];
  const replacementResults = [];
  const staleScanner = new AssetScanner(config((code) => staleResults.push(code)));
  const replacementScanner = new AssetScanner(config((code) => replacementResults.push(code)));

  await staleScanner.start();
  await replacementScanner.start();
  assert.equal(readers.length, 2);

  // The old instance remains active in the DOM-free simulation, while the new
  // instance has claimed the same reader ID. Its callback must be ignored.
  readers[0].onSuccess('STALE');
  readers[1].onSuccess('LIVE');
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(staleResults, [], 'a stale instance must not receive callbacks after ownership changes');
  assert.deepEqual(replacementResults, ['LIVE']);
});

test('a stale pending start cannot mutate the replacement scanner generation', async () => {
  readers.length = 0;
  stopResolvers.length = 0;
  startResolvers.length = 0;
  deferNextStart = true;
  const results = [];
  const scanner = new AssetScanner(config((code) => results.push(code)));

  const staleStart = scanner.start();
  scanner.stop();
  await scanner.start();
  assert.equal(readers.length, 2);
  assert.equal(startResolvers.length, 1);

  startResolvers[0]();
  await staleStart;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(stopResolvers.length, 1, 'the stale reader is cleaned up');
  stopResolvers[0]();
  await new Promise((resolve) => setImmediate(resolve));

  readers[0].onSuccess('STALE');
  readers[1].onSuccess('LIVE');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(results, ['LIVE']);
});

test('audit scanner entrypoint opens the rendered audit overlay', async () => {
  document.listeners.get('DOMContentLoaded')();
  const openHandler = elements.get('audit-open-scanner-btn').listeners.get('click');
  assert.equal(typeof openHandler, 'function');
  openHandler();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(elements.get('audit-scanner-modal').classList.contains('is-open'), true);
  assert.equal(typeof readers.at(-1).onSuccess, 'function');
});

test('audit scanner entrypoint defers when the audit basket owns the overlay', () => {
  const openButton = elements.get('audit-open-scanner-btn');
  openButton.listeners.clear();
  delete openButton.dataset.scannerInitialized;
  elements.set('audit-basket-root', new FakeElement('audit-basket-root'));

  document.listeners.get('DOMContentLoaded')();

  assert.equal(openButton.listeners.size, 0);
  elements.delete('audit-basket-root');
});

test('global scanner errors stay in the open overlay live region', async () => {
  document.listeners.get('DOMContentLoaded')();
  const openHandler = elements.get('global-open-scanner-btn').listeners.get('click');
  assert.equal(typeof openHandler, 'function');
  openHandler();
  await new Promise((resolve) => setImmediate(resolve));

  const reader = readers.at(-1);
  assert.equal(typeof reader.onSuccess, 'function');
  reader.onSuccess('TAG-404');
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  const feedback = elements.get('global-scanner-feedback');
  assert.equal(feedback.textContent, 'No asset matches: TAG-404');
  assert.equal(feedback.classList.contains('is-visible'), true);
  assert.equal(feedback.getAttribute('role'), 'alert');
  assert.equal(feedback.getAttribute('aria-live'), 'assertive');
});

test('a stale global lookup cannot stop or navigate the replacement scanner', async () => {
  readers.length = 0;
  stopResolvers.length = 0;
  startResolvers.length = 0;
  deferNextStart = false;

  const pendingResponses = [];
  const previousFetch = globalThis.fetch;
  const feedback = elements.get('global-scanner-feedback');
  feedback.textContent = '';
  feedback.classList.remove('is-visible');
  delete window.location.href;
  globalThis.fetch = () => new Promise((resolve) => pendingResponses.push(resolve));

  try {
    const openHandler = elements.get('global-open-scanner-btn').listeners.get('click');
    const closeHandler = elements.get('global-close-scanner-btn').listeners.get('click');
    assert.equal(typeof openHandler, 'function');
    assert.equal(typeof closeHandler, 'function');

    openHandler();
    await new Promise((resolve) => setImmediate(resolve));
    const staleReader = readers.at(-1);
    staleReader.onSuccess('STALE-TAG');
    assert.equal(pendingResponses.length, 1);

    closeHandler();
    openHandler();
    await new Promise((resolve) => setImmediate(resolve));
    const replacementReader = readers.at(-1);
    assert.notEqual(replacementReader, staleReader);

    // Finish the old camera stop while the replacement reader is active.
    assert.equal(stopResolvers.length, 1);
    stopResolvers[0]();
    await new Promise((resolve) => setImmediate(resolve));

    pendingResponses[0]({
      ok: true,
      json: () => Promise.resolve({ found: true, url: '/stale-target' }),
    });
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(replacementReader.isScanning, true, 'stale lookup must not stop the replacement');
    assert.equal(replacementReader.cleared, false, 'stale lookup must not clear the replacement');
    assert.equal(window.location.href, undefined, 'stale lookup must not navigate');
    assert.equal(feedback.textContent, '', 'stale lookup must not write replacement feedback');
  } finally {
    globalThis.fetch = previousFetch;
  }
});

const { nativeDetectorCoversFormats, SCANNER_FORMATS } = await import('./.build/scanner.mjs');

test('the native detector is only used when it covers every scanner format', () => {
  assert.equal(nativeDetectorCoversFormats([...SCANNER_FORMATS]), true);

  // Firefox ships a detector that answers QR codes and drops the 1D formats.
  assert.equal(nativeDetectorCoversFormats(['qr_code']), false);
  assert.equal(nativeDetectorCoversFormats(['qr_code', 'ean_13', 'ean_8']), false);

  // One missing format is enough to fall back to the bundled decoder.
  const withoutEan = SCANNER_FORMATS.filter((format) => format !== 'ean_13');
  assert.equal(nativeDetectorCoversFormats(withoutEan), false);

  // Nothing reported at all never qualifies.
  assert.equal(nativeDetectorCoversFormats([]), false);
});
