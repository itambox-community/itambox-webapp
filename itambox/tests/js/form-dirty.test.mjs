import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { transformSync } from 'esbuild';

const source = transformSync(readFileSync(new URL('../../static/src/form-dirty.ts', import.meta.url), 'utf8'), { loader: 'ts' }).code;

class FakeEvent {
  constructor(type, detail = {}) {
    this.type = type;
    this.detail = detail;
    this.defaultPrevented = false;
  }

  preventDefault() {
    this.defaultPrevented = true;
  }
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
    return !event.defaultPrevented;
  }
}

function harness({ initialCacheMiss = false } = {}) {
  let dirty = false;
  let confirmResult = false;
  let confirmationCount = 0;
  let currentContent = 'page A';
  const body = new FakeEventTarget();
  const document = {
    body,
    addEventListener() {},
    querySelectorAll(selector) {
      assert.equal(selector, 'form[data-dirty="true"]');
      return dirty ? [form] : [];
    },
  };
  const form = {
    offsetWidth: 100,
    offsetHeight: 0,
    getClientRects: () => [{}],
    hasAttribute: name => name === 'data-no-dirty-track' ? false : false,
    getAttribute: name => name === 'method' ? 'post' : null,
  };
  const window = new FakeEventTarget();
  window.location = { href: 'https://example.test/a' };

  const entries = [{
    url: window.location.href,
    state: null,
    content: 'page A',
    cacheMiss: initialCacheMiss,
  }];
  let index = 0;
  let historyPushCount = 0;
  let historyGoCount = 0;
  const history = {
    get state() {
      return entries[index].state;
    },
    get length() {
      return entries.length;
    },
    pushState(state, _title, url) {
      historyPushCount += 1;
      entries.splice(index + 1);
      const absoluteUrl = new URL(url, window.location.href).href;
      entries.push({ url: absoluteUrl, state, content: currentContent, cacheMiss: false });
      index = entries.length - 1;
      window.location.href = absoluteUrl;
    },
    replaceState(state, _title, url) {
      const absoluteUrl = new URL(url, window.location.href).href;
      entries[index] = { ...entries[index], state, url: absoluteUrl };
      window.location.href = absoluteUrl;
    },
    go(delta) {
      historyGoCount += 1;
      const target = index + delta;
      if (target < 0 || target >= entries.length) return;
      index = target;
      window.location.href = entries[index].url;
      const event = { state: entries[index].state };
      for (const listener of window.listeners.get('popstate') || []) listener(event);
      if (window.onpopstate) window.onpopstate(event);
    },
  };
  window.history = history;
  window.onpopstate = event => {
    if (!event.state?.htmx) return;
    // htmx saves the current page and replaces the target entry state before firing its history event.
    history.replaceState({ htmx: true }, '', window.location.href);
    const entry = entries[index];
    const eventName = entry.cacheMiss ? 'htmx:historyCacheMiss' : 'htmx:historyCacheHit';
    const restored = new FakeEvent(eventName, { path: new URL(entry.url).pathname });
    body.dispatchEvent(restored);
    if (!restored.defaultPrevented) {
      dirty = false;
      currentContent = entry.content;
      body.dispatchEvent(new FakeEvent('htmx:historyRestore', { path: new URL(entry.url).pathname, cacheMiss: entry.cacheMiss }));
    }
  };

  const context = vm.createContext({
    document,
    window,
    history,
    confirm() {
      confirmationCount += 1;
      return confirmResult;
    },
    gettext: text => text,
  });
  vm.runInContext(source, context);

  return {
    acceptHtmxNavigation(url, content) {
      // htmx saves the current page before performing its history update.
      history.replaceState({ htmx: true }, '', window.location.href);
      body.dispatchEvent(new FakeEvent('htmx:beforeHistoryUpdate', { history: { type: 'push', path: url } }));
      history.pushState({ htmx: true }, '', url);
      entries[index].content = content;
      currentContent = content;
      body.dispatchEvent(new FakeEvent('htmx:pushedIntoHistory', { path: url }));
    },
    replaceHtmxHistory(url, content) {
      history.replaceState({ htmx: true }, '', window.location.href);
      body.dispatchEvent(new FakeEvent('htmx:beforeHistoryUpdate', { history: { type: 'replace', path: url } }));
      history.replaceState({ htmx: true }, '', url);
      entries[index].content = content;
      currentContent = content;
      body.dispatchEvent(new FakeEvent('htmx:replacedInHistory', { path: url }));
    },
    acceptBack() {
      confirmResult = true;
      history.go(-1);
    },
    back() {
      history.go(-1);
    },
    forward() {
      history.go(1);
    },
    setDirty(value) {
      dirty = value;
    },
    unmarkHistoryEntry(entryIndex) {
      entries[entryIndex].state = { htmx: true };
    },
    setConfirmResult(value) {
      confirmResult = value;
    },
    get confirmationCount() {
      return confirmationCount;
    },
    get content() {
      return currentContent;
    },
    get historyLength() {
      return history.length;
    },
    get historyIndex() {
      return index;
    },
    get historyPushCount() {
      return historyPushCount;
    },
    get historyGoCount() {
      return historyGoCount;
    },
    get url() {
      return new URL(window.location.href).pathname;
    },
    dispatch(type, detail) {
      const event = new FakeEvent(type, detail);
      body.dispatchEvent(event);
      return event;
    },
  };
}

test('declining a cache-hit Back keeps accepted content and the matching URL', () => {
  const page = harness();
  page.acceptHtmxNavigation('/b', 'page B');
  page.setDirty(true);

  page.back();

  assert.equal(page.content, 'page B');
  assert.equal(page.url, '/b');
  assert.equal(page.confirmationCount, 1);
});

test('declining a cache-miss Back keeps accepted content and the matching URL', () => {
  const page = harness({ initialCacheMiss: true });
  page.acceptHtmxNavigation('/b', 'page B');
  page.setDirty(true);

  page.back();

  assert.equal(page.content, 'page B');
  assert.equal(page.url, '/b');
  assert.equal(page.confirmationCount, 1);
});

test('accepted boosted navigation updates the saved URL before a declined unindexed restore', () => {
  const page = harness();
  page.acceptHtmxNavigation('/b', 'page B');
  page.unmarkHistoryEntry(0);
  page.setDirty(true);

  page.back();

  assert.equal(page.content, 'page B');
  assert.equal(page.url, '/b');
  assert.equal(page.historyLength, 2);
});

test('accepted replaced navigation updates the saved URL before a declined unindexed restore', () => {
  const page = harness();
  page.acceptHtmxNavigation('/b', 'page B');
  page.replaceHtmxHistory('/c', 'page C');
  page.unmarkHistoryEntry(0);
  page.setDirty(true);

  page.back();

  assert.equal(page.content, 'page C');
  assert.equal(page.url, '/c');
  assert.equal(page.historyLength, 2);
});

test('accepted history navigation updates the location and a declined Forward returns without adding entries', () => {
  const page = harness();
  page.acceptHtmxNavigation('/b', 'page B');
  page.setDirty(true);
  page.acceptBack();
  page.setDirty(true);
  page.setConfirmResult(false);

  page.forward();

  assert.equal(page.content, 'page A');
  assert.equal(page.url, '/a');
  assert.equal(page.historyIndex, 0);
  assert.equal(page.historyLength, 2);
  assert.equal(page.confirmationCount, 2);
});

test('repeated declined Back navigations return to the same entry without pushing replacements', () => {
  const page = harness();
  page.acceptHtmxNavigation('/b', 'page B');
  page.setDirty(true);

  for (let attempt = 0; attempt < 3; attempt += 1) {
    page.back();
    assert.equal(page.content, 'page B');
    assert.equal(page.url, '/b');
    assert.equal(page.historyIndex, 1);
    assert.equal(page.historyLength, 2);
    assert.equal(page.historyPushCount, 1);
  }

  assert.equal(page.confirmationCount, 3);
  assert.equal(page.historyGoCount, 6);
});

test('a declined boosted navigation does not record its rejected destination', () => {
  const page = harness();
  page.setDirty(true);

  const event = page.dispatch('htmx:beforeSwap', {
    boosted: true,
    requestConfig: { method: 'get' },
    elt: null,
  });

  assert.equal(event.defaultPrevented, true);
  assert.equal(page.url, '/a');
  assert.equal(page.historyLength, 1);
});
