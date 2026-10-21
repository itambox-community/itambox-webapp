/**
 * ITAMbox theme preference: light, dark or automatic (follow the OS).
 *
 * The account preference is authoritative. The server renders it onto
 * <html data-theme-preference="..."> and the inline script in base.html /
 * base_public.html applies it before the stylesheets load, so there is no
 * flash of the wrong theme. This module owns the interactive part:
 *
 * - the segmented control in the user menu picks one of the three values and
 *   persists the choice to the account (localStorage is not consulted at all,
 *   so a stale value left by another user on the same browser can never win);
 * - an automatic preference follows `prefers-color-scheme` and reacts to an
 *   operating-system change without a reload.
 *
 * The resolution rules below mirror the inline pre-CSS script. Keep both in
 * step: the inline copy exists only because loading a module would be too late
 * for the first paint.
 */

export type ThemePreference = 'light' | 'dark' | 'auto';
export type ResolvedTheme = 'light' | 'dark';

/** Document attribute carrying the stored account preference. */
export const PREFERENCE_ATTRIBUTE = 'data-theme-preference';
/** Document attribute carrying the preferences API endpoint (absent when anonymous). */
export const CONFIG_URL_ATTRIBUTE = 'data-theme-config-url';
/**
 * The segmented control's radio inputs, one per preference.
 *
 * Matched by prefix on purpose: the menu renders in both shells, so each render
 * owns its own radio group (`theme-preference-<scope>`). A shared name would put
 * every segment of both shells into one group, and checking the second render
 * would silently uncheck the first.
 */
export const PREFERENCE_INPUT_SELECTOR = 'input[name^="theme-preference"]';

const DARK_QUERY = '(prefers-color-scheme: dark)';

/** Accept only the three supported values; anything else means automatic. */
export function normalizePreference(value: string | null | undefined): ThemePreference {
  if (value === 'light' || value === 'dark' || value === 'auto') {
    return value;
  }
  return 'auto';
}

/** Resolve a preference to the theme actually painted on the document. */
export function resolveTheme(preference: ThemePreference, prefersDark: boolean): ResolvedTheme {
  if (preference === 'auto') {
    return prefersDark ? 'dark' : 'light';
  }
  return preference;
}

function systemPrefersDark(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(DARK_QUERY).matches;
}

function currentPreference(): ThemePreference {
  return normalizePreference(document.documentElement.getAttribute(PREFERENCE_ATTRIBUTE));
}

/** Keep the segmented control in step with the preference actually in force. */
function syncControl(preference: ThemePreference): void {
  document.querySelectorAll<HTMLInputElement>(PREFERENCE_INPUT_SELECTOR).forEach(function (input) {
    input.checked = input.value === preference;
  });
}

function applyPreference(preference: ThemePreference): void {
  const root = document.documentElement;
  root.setAttribute(PREFERENCE_ATTRIBUTE, preference);
  root.setAttribute('data-bs-theme', resolveTheme(preference, systemPrefersDark()));
  syncControl(preference);
}

function persistPreference(preference: ThemePreference): void {
  const url = document.documentElement.getAttribute(CONFIG_URL_ATTRIBUTE);
  if (!url) {
    // Anonymous visitor: there is no account to store the choice on.
    return;
  }
  const meta = document.querySelector<HTMLMetaElement>('meta[name=csrf-token]');
  const token = meta ? meta.content : '';
  fetch(url, {
    method: 'PATCH',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': token,
    },
    body: JSON.stringify({ theme: { theme: preference } }),
  }).catch(function () {
    // Offline or rejected: the stored account preference stays authoritative
    // on the next render.
  });
}

function initTheme(): void {
  applyPreference(currentPreference());

  document.addEventListener('change', function (event) {
    const target = event.target as HTMLInputElement | null;
    if (!target || !target.matches(PREFERENCE_INPUT_SELECTOR)) {
      return;
    }
    const preference = normalizePreference(target.value);
    applyPreference(preference);
    persistPreference(preference);
  });

  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener('change', function () {
    if (currentPreference() === 'auto') {
      applyPreference('auto');
    }
  });

  // Boosted navigation swaps the menu markup, so re-point the control at the
  // preference in force instead of leaving a stale server-rendered selection.
  document.addEventListener('htmx:afterSettle', function () {
    syncControl(currentPreference());
  });
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', initTheme);
}
