import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import test from 'node:test';

const itamboxRoot = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const buildDirectory = resolve(itamboxRoot, 'tests/js/.build');
const buildPath = resolve(buildDirectory, 'object-detail-refresh.mjs');

function buildObjectDetail() {
  mkdirSync(buildDirectory, { recursive: true });
  const esbuild = process.env.ITAMBOX_ESBUILD_BIN || resolve(
    itamboxRoot,
    'node_modules/.bin',
    process.platform === 'win32' ? 'esbuild.cmd' : 'esbuild',
  );
  execFileSync(
    esbuild,
    [
      'static/src/object-detail.ts',
      '--bundle',
      '--format=esm',
      '--platform=browser',
      `--outfile=${buildPath}`,
      '--log-level=warning',
    ],
    { cwd: itamboxRoot, stdio: 'ignore', shell: process.platform === 'win32' },
  );
}

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }
}

class FakeTab {
  constructor(attributes, active = false) {
    this.attributes = attributes;
    this.active = active;
    this.showCalled = false;
    this.classList = { contains: (name) => name === 'active' && this.active };
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
}

class FakeDocument {
  constructor(hasListContent, tabs = [], panes = {}) {
    this.readyState = 'loading';
    this.body = new FakeEventTarget();
    this.hasListContent = hasListContent;
    this.tabs = tabs;
    this.panes = panes;
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }

  getElementById(id) {
    return id === 'object-list-dynamic-content' && this.hasListContent ? {} : null;
  }

  querySelector(selector) {
    if (this.panes[selector]) return this.panes[selector];
    if (selector.includes('a[href="#unrelated"]')) {
      return this.tabs.find((tab) => tab.getAttribute('href') === '#unrelated');
    }
    if ((selector.match(/"/g) || []).length % 2) throw new SyntaxError('Invalid selector');
    const match = selector.match(/^a\[(data-bs-target|href|hx-get)="([^"]*)"\]$/);
    return match ? this.tabs.find((tab) => tab.getAttribute(match[1]) === match[2]) || null : null;
  }

  querySelectorAll(selector) {
    return selector === 'a' ? this.tabs : [];
  }
}

async function loadObjectDetail({ hasListContent, tab, tabs = [], panes = {} }) {
  const document = new FakeDocument(hasListContent, tabs, panes);
  const ajaxCalls = [];
  const triggerCalls = [];
  let reloadCount = 0;
  const window = new FakeEventTarget();
  const search = tab === undefined ? '' : `?${new URLSearchParams({ tab }).toString()}`;
  window.location = {
    href: `http://127.0.0.1/assets/1/${search}`,
    search,
    reload() {
      reloadCount += 1;
    },
  };
  window.history = { replaceState() {} };
  globalThis.document = document;
  globalThis.window = window;
  globalThis.htmx = {
    ajax(...args) {
      ajaxCalls.push(args);
    },
    trigger(...args) {
      triggerCalls.push(args);
    },
  };
  globalThis.bootstrap = {
    Tab: {
      getOrCreateInstance(target) {
        return { show: () => { target.showCalled = true; } };
      },
    },
  };
  buildObjectDetail();
  await import(`${pathToFileURL(buildPath).href}?case=${hasListContent ? 'list' : 'detail'}-${Date.now()}-${Math.random()}`);
  return { document, ajaxCalls, triggerCalls, reloadCount: () => reloadCount };
}

test('list refresh events do not trigger a duplicate detail reload', async () => {
  const { document, ajaxCalls, reloadCount } = await loadObjectDetail({ hasListContent: true });

  document.body.dispatchEvent({ type: 'tableRefreshRequired' });
  document.body.dispatchEvent({ type: 'licenseUpdated' });

  assert.equal(ajaxCalls.length, 0);
  assert.equal(reloadCount(), 0);
});

test('detail refresh events use the HTMX detail request when no list is mounted', async () => {
  const { document, ajaxCalls, reloadCount } = await loadObjectDetail({ hasListContent: false });

  document.body.dispatchEvent({ type: 'tableRefreshRequired' });

  assert.deepEqual(ajaxCalls, [['GET', 'http://127.0.0.1/assets/1/', { target: 'body', swap: 'outerHTML' }]]);
  assert.equal(reloadCount(), 0);
});

test('malformed and unknown tab values leave the default tab active', async () => {
  const malformedValues = ['"', ']', '\\', '%22', 'not-a-tab', 'missing"] , a[href="#unrelated"] /*'];
  for (const tab of malformedValues) {
    const defaultTab = new FakeTab({ href: '#details' }, true);
    const unrelatedTab = new FakeTab({ href: '#unrelated' });
    const { document } = await loadObjectDetail({ hasListContent: false, tab, tabs: [defaultTab, unrelatedTab] });

    assert.doesNotThrow(() => document.dispatchEvent({ type: 'DOMContentLoaded' }), `tab=${tab}`);
    assert.equal(defaultTab.classList.contains('active'), true);
    assert.equal(unrelatedTab.showCalled, false, `tab=${tab} selected an unrelated element`);
  }
});

test('valid lazy tab deep links still activate and load their pane', async () => {
  const lazyTab = new FakeTab({ href: '?tab=history', 'hx-get': '?tab=history', 'data-bs-target': '#history-pane' });
  const pane = { querySelector: (selector) => selector === '.spinner-border' ? {} : null };
  const { document, triggerCalls } = await loadObjectDetail({
    hasListContent: false,
    tab: 'history',
    tabs: [lazyTab],
    panes: { '#history-pane': pane },
  });

  document.dispatchEvent({ type: 'DOMContentLoaded' });

  assert.equal(lazyTab.showCalled, true);
  assert.deepEqual(triggerCalls, [[lazyTab, 'click']]);
});
