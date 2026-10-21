/**
 * Unit tests for the strict non-boost guard (issue #317).
 *
 * The guard's decision seam is `isAppExternal`: everything the browser-side
 * click handler does is derive from it, so the policy is tested at its real
 * boundary without a DOM. The click interception itself (capture-phase
 * preventDefault + location.assign, before htmx's body-level listener) is
 * intentionally DOM-free by construction and verified manually in the browser.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { NON_APP_PATH_PREFIXES, isAppExternal } from './.build/boost-guard.mjs';

const ORIGIN = 'https://demo.itambox.dev';

test('docs pages are app-external (the #317 repro link)', () => {
  assert.equal(
    isAppExternal('https://github.com/itambox-community/design-docs/blob/main/development/capability-registry.md', ORIGIN),
    true,
  );
  assert.equal(isAppExternal('https://demo.itambox.dev/static/docs/index.html', ORIGIN), true);
});

test('api, graphql, admin, accounts and media paths are app-external', () => {
  assert.equal(isAppExternal('https://demo.itambox.dev/api/', ORIGIN), true);
  assert.equal(isAppExternal('https://demo.itambox.dev/api/schema/', ORIGIN), true);
  assert.equal(isAppExternal('https://demo.itambox.dev/graphql', ORIGIN), true);
  assert.equal(isAppExternal('https://demo.itambox.dev/admin/', ORIGIN), true);
  assert.equal(isAppExternal('https://demo.itambox.dev/accounts/login/?next=/', ORIGIN), true);
  assert.equal(isAppExternal('https://demo.itambox.dev/media/uploads/report.pdf', ORIGIN), true);
});

test('cross-origin links are app-external regardless of path', () => {
  assert.equal(isAppExternal('https://other.example/static/docs/', ORIGIN), true);
  assert.equal(isAppExternal('https://demo.itambox.dev.evil.example/', ORIGIN), true);
});

test('scheme differences count as external (htmx only compares hostname)', () => {
  assert.equal(isAppExternal('http://demo.itambox.dev/', ORIGIN), true);
});

test('in-app routes are not app-external and stay boosted', () => {
  assert.equal(isAppExternal('https://demo.itambox.dev/', ORIGIN), false);
  assert.equal(isAppExternal('https://demo.itambox.dev/extras/alerts/channels/', ORIGIN), false);
  assert.equal(isAppExternal('https://demo.itambox.dev/organization/roles/1/', ORIGIN), false);
});

test('non-http(s) protocols are left untouched', () => {
  assert.equal(isAppExternal('mailto:ops@itambox.dev', ORIGIN), false);
  assert.equal(isAppExternal('javascript:void(0)', ORIGIN), false);
});

test('the guard covers the documented app-external prefixes', () => {
  for (const prefix of ['/static/', '/api/', '/graphql', '/admin/', '/accounts/']) {
    assert.ok(NON_APP_PATH_PREFIXES.includes(prefix), `missing prefix ${prefix}`);
  }
});
