import type { ReactNode } from 'react';

import { TopBar } from '@/shell/TopBar';

/**
 * The frame every screen is rendered inside.
 *
 * There is no router yet and that is deliberate: `/command` is currently the
 * only screen, and a router installed before a second route would be a
 * dependency chosen for a page that does not exist. It lands with the incident
 * detail screen, which is the first thing that genuinely needs a URL of its
 * own — and adding it then is a recorded decision, per the no-new-runtime-
 * dependency rule.
 */
export function CommandShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      {/* First stop on the tab order: a keyboard user should not have to walk
          the header to reach the incident that woke them up. */}
      <a
        href="#main"
        className="bg-accent text-accent-contrast sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-20 focus:rounded focus:px-3 focus:py-2"
      >
        Skip to content
      </a>

      <TopBar />

      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">
        {children}
      </main>
    </div>
  );
}
