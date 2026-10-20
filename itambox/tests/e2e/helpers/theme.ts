/**
 * Theme preference helpers for the end-to-end specs (issue #908).
 *
 * The account preference is the source of truth, so a spec that needs a known
 * starting state sets it through the same preferences endpoint the topbar
 * toggle uses, then reloads to pick up the server-rendered document.
 */
import { expect, type Page } from '@playwright/test';

export type ThemePreference = 'light' | 'dark' | 'auto';

export interface StoredThemeConfig {
  theme?: { theme?: string };
}

function configUrl(page: Page): Promise<string | null> {
  return page.locator('html').getAttribute('data-theme-config-url');
}

export async function setThemePreference(page: Page, preference: ThemePreference): Promise<void> {
  const url = await configUrl(page);
  if (!url) throw new Error('The document does not expose a theme preferences endpoint.');
  const token = await page.evaluate(() => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta as HTMLMetaElement).content : '';
  });
  const response = await page.request.patch(url, {
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token },
    data: { theme: { theme: preference } },
  });
  expect(response.status(), `PATCH ${url}: ${await response.text()}`).toBe(200);
}

/**
 * Pick a theme through the user-facing control (the segmented switch in the
 * user menu), leaving the menu open state alone so repeated calls work.
 */
export async function chooseThemeInUserMenu(page: Page, preference: ThemePreference): Promise<void> {
  // The user menu renders in the desktop topbar and in the mobile header, so the
  // switch ids carry their shell and the desktop specs address the topbar one.
  const topbar = page.locator('.itambox-topbar');
  const label = topbar.locator(`label[for="theme-preference-topbar-${preference}"]`);
  if (!(await label.isVisible())) {
    await topbar.getByRole('link', { name: 'Open user menu' }).click();
  }
  await label.click();
}

export async function readStoredThemePreference(page: Page): Promise<string | undefined> {
  const url = await configUrl(page);
  if (!url) throw new Error('The document does not expose a theme preferences endpoint.');
  const response = await page.request.get(url);
  expect(response.status(), `GET ${url}: ${await response.text()}`).toBe(200);
  const body = (await response.json()) as { data?: StoredThemeConfig };
  return body.data?.theme?.theme;
}

export async function readDocumentTheme(page: Page): Promise<{
  preference: string | null;
  resolved: string | null;
}> {
  return {
    preference: await page.locator('html').getAttribute('data-theme-preference'),
    resolved: await page.locator('html').getAttribute('data-bs-theme'),
  };
}
