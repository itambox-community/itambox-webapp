import assert from 'node:assert/strict';
import test from 'node:test';
import { buildGraphiQLProps, readCsrfHeaders, readInitialHash } from '../../static/src/graphql-ui-config.js';

// GraphiQL v5 throws on first edit when handed the removed controlled props.
const REMOVED_PROPS = ['query', 'variables', 'operationName', 'onEditQuery', 'onEditVariables', 'onEditOperationName'];

function fakeLocation(hash) {
  return { hash, pathname: '/graphql/', search: '?a=1' };
}

test('hash is read once and stripped from the URL', () => {
  const calls = [];
  const initial = readInitialHash(fakeLocation('#query=%7Bme%7D&variables=%7B%7D'), {
    replaceState: (...args) => calls.push(args),
  });
  assert.equal(initial.query, '{me}');
  assert.equal(initial.variables, '{}');
  assert.deepEqual(calls, [[null, '', '/graphql/?a=1']]);
});

test('an empty hash leaves history untouched', () => {
  const calls = [];
  const initial = readInitialHash(fakeLocation(''), { replaceState: (...a) => calls.push(a) });
  assert.deepEqual(initial, { query: undefined, variables: undefined });
  assert.equal(calls.length, 0);
});

test('props use initial* values and none of the removed props', () => {
  const props = buildGraphiQLProps({
    fetcher: () => {},
    initial: { query: '{me}', variables: '{"a":1}' },
    settings: { graphiqlInputValueDeprecation: true },
  });
  assert.equal(props.initialQuery, '{me}');
  assert.equal(props.initialVariables, '{"a":1}');
  assert.equal(props.inputValueDeprecation, true);
  for (const name of REMOVED_PROPS) assert.ok(!(name in props), `${name} must not be passed`);
});

test('no initial props are set without a hash', () => {
  const props = buildGraphiQLProps({ fetcher: () => {}, initial: {}, settings: {} });
  assert.ok(!('initialQuery' in props));
  assert.ok(!('initialVariables' in props));
});

test('csrf header comes from the cookie, then the form field', () => {
  assert.deepEqual(readCsrfHeaders({ cookie: 'x=1; csrftoken=abc', querySelector: () => null }), { 'X-CSRFToken': 'abc' });
  assert.deepEqual(readCsrfHeaders({ cookie: '', querySelector: () => ({ value: 'zzz' }) }), { 'X-CSRFToken': 'zzz' });
  assert.deepEqual(readCsrfHeaders({ cookie: '', querySelector: () => null }), {});
});
