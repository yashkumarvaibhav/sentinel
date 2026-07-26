import { useCallback, useEffect, useState } from 'react';

export type ThemePreference = 'system' | 'light' | 'dark';

const STORAGE_KEY = 'sentinel.theme';

function readStored(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    // A browser with storage denied still gets a working toggle; it just
    // forgets. Throwing here would take the whole shell down with it.
    return 'system';
  }
}

/**
 * The theme, and the one place the root element's `data-theme` is written.
 *
 * `system` is a real third state rather than a default that resolves to one of
 * the other two: a user who has never chosen should keep following their OS
 * when it changes at sunset, and that is only expressible by storing nothing.
 * The stylesheet reads the same absence — `:root:not([data-theme='light'])`
 * inside the dark media query — so the CSS and this hook agree by construction.
 */
export function useTheme(): {
  preference: ThemePreference;
  setPreference: (next: ThemePreference) => void;
  cycle: () => void;
} {
  const [preference, setPreferenceState] = useState<ThemePreference>(readStored);

  useEffect(() => {
    const root = document.documentElement;
    if (preference === 'system') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', preference);
    }
  }, [preference]);

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    try {
      if (next === 'system') {
        window.localStorage.removeItem(STORAGE_KEY);
      } else {
        window.localStorage.setItem(STORAGE_KEY, next);
      }
    } catch {
      // Same reasoning as the read: a forgetful toggle beats a broken page.
    }
  }, []);

  const cycle = useCallback(() => {
    setPreference(
      preference === 'system' ? 'light' : preference === 'light' ? 'dark' : 'system',
    );
  }, [preference, setPreference]);

  return { preference, setPreference, cycle };
}
