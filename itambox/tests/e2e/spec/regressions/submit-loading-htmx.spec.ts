import { createServer, type Server, type ServerResponse } from 'node:http';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { transformSync } from 'esbuild';
import { expect, test, type Page } from '@playwright/test';

const repositoryRoot = resolve(__dirname, '../../../..');
const htmxSource = readFileSync(resolve(repositoryRoot, 'node_modules/htmx.org/dist/htmx.min.js'), 'utf8');
const submitLoadingSource = transformSync(
  readFileSync(resolve(repositoryRoot, 'static/src/form-submit-loading.ts'), 'utf8'),
  { loader: 'ts', format: 'iife' },
).code;

let server: Server;
let origin: string;

function respond(response: ServerResponse, status: number, contentType: string, body: string): void {
  response.writeHead(status, { 'content-type': contentType });
  response.end(body);
}

async function openTestPage(page: Page): Promise<void> {
  await page.goto(origin, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => Boolean((window as Window & { htmx?: unknown }).htmx));
}

async function holdHtmxResponse(page: Page, status: number) {
  let requests = 0;
  let releaseResponse: () => void = () => undefined;
  const responseReleased = new Promise<void>((resolveResponse) => {
    releaseResponse = resolveResponse;
  });
  let firstRequestReceived: () => void = () => undefined;
  const firstRequest = new Promise<void>((resolveRequest) => {
    firstRequestReceived = resolveRequest;
  });

  await page.route('**/submit', async (route) => {
    requests += 1;
    if (requests === 1) firstRequestReceived();
    await responseReleased;
    await route.fulfill({ status, body: '' });
  });

  return { firstRequest, releaseResponse, requestCount: () => requests };
}

test.describe('submit-loading guard with the shipped HTMX runtime @anonymous', () => {
  test.beforeAll(async () => {
    const html = `<!doctype html>
      <html><head><meta charset="utf-8"><title>Submit loading integration</title></head>
      <body>
        <form id="htmx-form" hx-post="/submit" hx-swap="none">
          <input id="htmx-input" name="value" value="test">
          <button id="htmx-button" type="submit">Save</button>
        </form>
        <form id="native-form" method="post" action="/native-submit">
          <input name="value" value="native">
          <button id="native-button" type="submit">Native save</button>
        </form>
        <script>window.gettext = (message) => message;</script>
        <script src="/htmx.js"></script>
        <script src="/submit-loading.js"></script>
      </body></html>`;

    server = createServer((request, response) => {
      const path = new URL(request.url || '/', 'http://127.0.0.1').pathname;
      if (request.method === 'GET' && path === '/') {
        respond(response, 200, 'text/html; charset=utf-8', html);
      } else if (request.method === 'GET' && path === '/htmx.js') {
        respond(response, 200, 'text/javascript; charset=utf-8', htmxSource);
      } else if (request.method === 'GET' && path === '/submit-loading.js') {
        respond(response, 200, 'text/javascript; charset=utf-8', submitLoadingSource);
      } else if (request.method === 'POST' && path === '/native-submit') {
        respond(response, 200, 'text/html; charset=utf-8', '<main>Native submission received</main>');
      } else {
        respond(response, 404, 'text/plain; charset=utf-8', 'Not found');
      }
    });
    await new Promise<void>((resolveListen, rejectListen) => {
      server.once('error', rejectListen);
      server.listen(0, '127.0.0.1', resolveListen);
    });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Test server did not bind a TCP port.');
    origin = `http://127.0.0.1:${address.port}/`;
  });

  test.afterAll(async () => {
    if (!server?.listening) return;
    await new Promise<void>((resolveClose, rejectClose) => {
      server.close((error) => (error ? rejectClose(error) : resolveClose()));
    });
  });

  test('drops repeated Enter submits while pending, re-arms after 204, and leaves the form mounted', async ({ page }) => {
    await openTestPage(page);
    const held = await holdHtmxResponse(page, 204);
    const form = page.locator('#htmx-form');
    const input = page.locator('#htmx-input');

    await input.focus();
    await page.keyboard.press('Enter');
    await held.firstRequest;
    await page.keyboard.press('Enter');
    expect(held.requestCount()).toBe(1);

    held.releaseResponse();
    await expect(form).not.toHaveAttribute('data-submitting', 'true');
    await expect(page.locator('#htmx-button')).not.toHaveClass(/pointer-events-none|disabled/);
    await page.waitForTimeout(100);
    expect(held.requestCount()).toBe(1);
    await expect(form).toBeAttached();

    await input.focus();
    await page.keyboard.press('Enter');
    await expect.poll(held.requestCount).toBe(2);
    await expect(form).not.toHaveAttribute('data-submitting', 'true');
  });

  test('drops repeated clicks while pending and re-arms after an HTTP failure', async ({ page }) => {
    await openTestPage(page);
    const held = await holdHtmxResponse(page, 500);
    const form = page.locator('#htmx-form');
    const button = page.locator('#htmx-button');

    await button.evaluate((element) => (element as HTMLButtonElement).click());
    await held.firstRequest;
    await button.evaluate((element) => (element as HTMLButtonElement).click());
    expect(held.requestCount()).toBe(1);

    held.releaseResponse();
    await expect(form).not.toHaveAttribute('data-submitting', 'true');
    await expect(button).not.toHaveClass(/pointer-events-none|disabled/);
    await page.waitForTimeout(100);
    expect(held.requestCount()).toBe(1);

    await button.evaluate((element) => (element as HTMLButtonElement).click());
    await expect.poll(held.requestCount).toBe(2);
    await expect(form).not.toHaveAttribute('data-submitting', 'true');
  });

  test('allows the first native form submission without an HTMX request header', async ({ page }) => {
    await openTestPage(page);
    const nativeRequest = page.waitForRequest((request) => request.url().endsWith('/native-submit'));
    const navigation = page.waitForNavigation({ waitUntil: 'domcontentloaded' });
    await page.locator('#native-button').click();

    const [request, response] = await Promise.all([nativeRequest, navigation]);
    expect(request.method()).toBe('POST');
    expect(request.headers()['hx-request']).toBeUndefined();
    expect(response?.status()).toBe(200);
    await expect(page.locator('main')).toHaveText('Native submission received');
  });
});
