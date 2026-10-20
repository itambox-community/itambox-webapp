/**
 * Issue #908: the theme preference (light / dark / automatic) must reach the
 * document on the first paint and the topbar toggle must persist the choice on
 * the account, not in localStorage.
 *
 * The automatic cases drive the operating-system preference through Playwright
 * media emulation: `prefers-color-scheme` is what the pre-CSS script and the
 * `change` listener both read.
 */
import { test, expect } from '@playwright/test';

import { readDocumentTheme, readStoredThemePreference, setThemePreference } from '../../helpers/theme';

test('automatic follows a dark operating system and reacts to a switch without reload', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await setThemePreference(page, 'auto');

  await page.emulateMedia({ colorScheme: 'dark' });
  await page.reload({ waitUntil: 'domcontentloaded' });

  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('dark');
  expect((await readDocumentTheme(page)).preference, 'the stored preference stays automatic').toBe('auto');

  // An operating-system switch applies live, without a reload.
  await page.emulateMedia({ colorScheme: 'light' });
  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('light');
  expect((await readDocumentTheme(page)).preference).toBe('auto');

  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('dark');

  await setThemePreference(page, 'auto');
});

test('the toggle cycles light, dark and automatic and stores the choice on the account', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await setThemePreference(page, 'light');
  await page.reload({ waitUntil: 'domcontentloaded' });

  const toggle = page.locator('.color-mode-toggle:visible').first();

  await expect.poll(async () => (await readDocumentTheme(page)).preference).toBe('light');
  await expect(toggle).toHaveAttribute('aria-label', 'Enable dark mode');

  // light -> dark
  await toggle.click();
  await expect.poll(async () => (await readDocumentTheme(page)).preference).toBe('dark');
  expect((await readDocumentTheme(page)).resolved).toBe('dark');
  await expect.poll(async () => readStoredThemePreference(page)).toBe('dark');
  await expect(toggle).toHaveAttribute('aria-label', 'Use system theme');

  // dark -> automatic (a light operating system resolves it back to light)
  await toggle.click();
  await expect.poll(async () => (await readDocumentTheme(page)).preference).toBe('auto');
  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('light');
  await expect.poll(async () => readStoredThemePreference(page)).toBe('auto');
  await expect(toggle).toHaveAttribute('aria-label', 'Enable light mode');

  // automatic -> light
  await toggle.click();
  await expect.poll(async () => (await readDocumentTheme(page)).preference).toBe('light');
  await expect.poll(async () => readStoredThemePreference(page)).toBe('light');

  // The account carries the choice into a new document.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(async () => (await readDocumentTheme(page)).preference).toBe('light');
  expect((await readDocumentTheme(page)).resolved).toBe('light');

  await setThemePreference(page, 'auto');
});

test('anonymous visitors follow the operating system and ignore a stale stored theme @anonymous', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/accounts/login/', { waitUntil: 'domcontentloaded' });

  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('dark');
  expect((await readDocumentTheme(page)).preference, 'anonymous pages resolve to automatic').toBe('auto');
  expect(await page.locator('html').getAttribute('data-theme-config-url')).toBeNull();

  // A localStorage value left behind by an earlier authenticated session is
  // not a source of truth any more.
  await page.evaluate(() => localStorage.setItem('itambox.theme', 'dark'));
  await page.emulateMedia({ colorScheme: 'light' });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('light');

  // The public layout still reacts to an operating-system switch.
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect.poll(async () => (await readDocumentTheme(page)).resolved).toBe('dark');
});
