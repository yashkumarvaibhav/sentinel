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
/** What the page is actually showing right now, with `system` resolved. */
export type ResolvedTheme = 'light' | 'dark';

function readSystem(): ResolvedTheme {
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

export function useTheme(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (next: ThemePreference) => void;
  toggle: () => void;
  cycle: () => void;
} {
  const [preference, setPreferenceState] = useState<ThemePreference>(readStored);
  const [system, setSystem] = useState<ResolvedTheme>(readSystem);

  // While the preference is `system`, the OS flipping at sunset must move the
  // page with it - and must also move the toggle's icon, or the control starts
  // offering the theme you are already looking at.
  useEffect(() => {
    let query: MediaQueryList;
    try {
      query = window.matchMedia('(prefers-color-scheme: dark)');
    } catch {
      return;
    }
    const onChange = () => setSystem(query.matches ? 'dark' : 'light');
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  const resolved: ResolvedTheme = preference === 'system' ? system : preference;

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

  /**
   * Flip to the opposite of what is on screen.
   *
   * Deliberately resolves `system` first rather than treating it as a third
   * stop: a user who has never chosen sees the theme their OS picked, and the
   * one thing they can want from a toggle is the other one. `system` therefore
   * survives as the state you start in rather than one you cycle back to
   * (BUILD_STATE decision #143, superseding #142).
   */
  const toggle = useCallback(() => {
    setPreference(resolved === 'dark' ? 'light' : 'dark');
  }, [resolved, setPreference]);

  const cycle = useCallback(() => {
    setPreference(
      preference === 'system' ? 'light' : preference === 'light' ? 'dark' : 'system',
    );
  }, [preference, setPreference]);

  return { preference, resolved, setPreference, toggle, cycle };
}
