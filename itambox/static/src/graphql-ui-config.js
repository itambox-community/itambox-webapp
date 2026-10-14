// Pure wiring for the GraphiQL page, kept free of DOM side effects at import
// time so it can be unit-tested. The "#query=" hash is read once and stripped,
// and GraphiQL receives plain initial values. GraphiQL v5 removed the controlled query/variables/
// operationName props, so nothing here is kept in React state or synced back
// to the URL on edit.

export function readInitialHash(location, history) {
  const parameters = new URLSearchParams(location.hash.slice(1));
  const initial = {
    query: parameters.get('query') || undefined,
    variables: parameters.get('variables') || undefined,
  };
  if (location.hash) {
    history.replaceState(null, '', `${location.pathname}${location.search}`);
  }
  return initial;
}

export function readCsrfHeaders(doc) {
  const token =
    doc.cookie.match(/(?:^|; )csrftoken=([^;]+)/)?.[1] ||
    doc.querySelector('[name="csrfmiddlewaretoken"]')?.value;
  return token ? { 'X-CSRFToken': token } : {};
}

export function buildGraphiQLProps({ fetcher, initial, settings }) {
  const props = {
    fetcher,
    defaultEditorToolsVisibility: true,
    isHeadersEditorEnabled: settings.graphiqlHeaderEditorEnabled,
    shouldPersistHeaders: settings.graphiqlShouldPersistHeaders,
    inputValueDeprecation: settings.graphiqlInputValueDeprecation,
  };
  if (initial.query) props.initialQuery = initial.query;
  if (initial.variables) props.initialVariables = initial.variables;
  return props;
}
