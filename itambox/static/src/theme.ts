/**
 * ITAMbox theme preference: light, dark or automatic (follow the OS).
 *
 * The account preference is authoritative. The server renders it onto
 * <html data-theme-preference="..."> and the inline script in base.html /
 * base_public.html applies it before the stylesheets load, so there is no
 * flash of the wrong theme. This module owns the interactive part:
 *
 * - the topbar toggle cycles light -> dark -> automatic and persists the new
 *   choice to the account (localStorage is not consulted at all, so a stale
 *   value left by another user on the same browser can never win);
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
/** Document attribute prefix carrying the label for a target preference. */
export const LABEL_ATTRIBUTE_PREFIX = 'data-theme-label-';

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

/** Cycle the toggle: light -> dark -> automatic -> light. */
export function nextPreference(preference: ThemePreference): ThemePreference {
  if (preference === 'light') {
    return 'dark';
  }
  if (preference === 'dark') {
    return 'auto';
  }
  return 'light';
}

function systemPrefersDark(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia(DARK_QUERY).matches;
}

function currentPreference(): ThemePreference {
  return normalizePreference(document.documentElement.getAttribute(PREFERENCE_ATTRIBUTE));
}

function updateToggleLabels(preference: ThemePreference): void {
  const label = document.documentElement.getAttribute(LABEL_ATTRIBUTE_PREFIX + nextPreference(preference));
  if (!label) {
    return;
  }
  document.querySelectorAll<HTMLElement>('.color-mode-toggle').forEach(function (button) {
    button.setAttribute('title', label);
    button.setAttribute('aria-label', label);
  });
}

function applyPreference(preference: ThemePreference): void {
  const root = document.documentElement;
  root.setAttribute(PREFERENCE_ATTRIBUTE, preference);
  root.setAttribute('data-bs-theme', resolveTheme(preference, systemPrefersDark()));
  updateToggleLabels(preference);
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

  document.addEventListener('click', function (event) {
    const target = event.target as HTMLElement | null;
    if (!target || !target.closest('.color-mode-toggle')) {
      return;
    }
    event.preventDefault();
    const preference = nextPreference(currentPreference());
    applyPreference(preference);
    persistPreference(preference);
  });

  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener('change', function () {
    if (currentPreference() === 'auto') {
      applyPreference('auto');
    }
  });

  // Boosted navigation swaps the topbar markup, so re-apply the labels after
  // the swap instead of leaving stale server-rendered labels in place.
  document.addEventListener('htmx:afterSettle', function () {
    updateToggleLabels(currentPreference());
  });
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', initTheme);
}
