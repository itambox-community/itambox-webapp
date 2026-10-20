/**
 * Unit tests for the theme preference resolution (issue #908).
 *
 * The decision seam is pure: `resolveTheme(preference, prefersDark)` answers
 * what the document should paint. It is tested without a DOM; the inline
 * pre-CSS script in base.html mirrors the same rules.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizePreference,
  resolveTheme,
} from './.build/theme.mjs';

test('an explicit light preference ignores the operating system', () => {
  assert.equal(resolveTheme('light', false), 'light');
  assert.equal(resolveTheme('light', true), 'light');
});

test('an explicit dark preference ignores the operating system', () => {
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('dark', true), 'dark');
});

test('automatic follows the operating system in both states', () => {
  assert.equal(resolveTheme('auto', true), 'dark');
  assert.equal(resolveTheme('auto', false), 'light');
});

test('unknown preference values fall back to automatic', () => {
  for (const value of [null, undefined, '', 'Dark', 'system', 'not-a-theme']) {
    assert.equal(normalizePreference(value), 'auto', `unexpected result for ${String(value)}`);
  }
});

test('supported preference values survive normalization', () => {
  for (const value of ['light', 'dark', 'auto']) {
    assert.equal(normalizePreference(value), value);
  }
});
