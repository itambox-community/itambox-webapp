import { createGraphiQLFetcher } from '@graphiql/toolkit';
import { GraphiQL } from 'graphiql';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { buildGraphiQLProps, readCsrfHeaders, readInitialHash } from './graphql-ui-config.js';

const settings = window.GRAPHIQL_SETTINGS || {};
const initial = readInitialHash(window.location, window.history);
const fetcher = createGraphiQLFetcher({
  url: `${window.location.pathname}${window.location.search}`,
  headers: readCsrfHeaders(document),
});

createRoot(document.getElementById('editor')).render(
  React.createElement(GraphiQL, buildGraphiQLProps({ fetcher, initial, settings })),
);
