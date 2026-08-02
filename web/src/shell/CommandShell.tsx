import { NavLink, Outlet } from 'react-router';

import { TopBar } from '@/shell/TopBar';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';

/**
 * The frame every screen is rendered inside.
 *
 * The router landed with the incident proof screen, the first second URL.
 * Navigation is intentionally limited to screens that exist now; future
 * information-architecture labels do not become misleading dead links.
 */
export function CommandShell() {
  return (
    <SnapshotStreamProvider>
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

        <nav aria-label="Primary" className="border-line bg-sidebar border-b">
          <div className="mx-auto flex max-w-6xl gap-1 px-4 py-2 sm:px-6">
            <NavLink
              className={({ isActive }) =>
                `min-h-11 rounded-md px-3 py-3 text-xs font-medium sm:min-h-0 sm:py-2 ${
                  isActive ? 'bg-accent-soft text-accent-hover' : 'text-muted hover:text-ink'
                }`
              }
              end
              to="/command"
            >
              Command
            </NavLink>
            <NavLink
              className="text-muted hover:text-ink min-h-11 rounded-md px-3 py-3 text-xs font-medium sm:min-h-0 sm:py-2"
              to="/command#live-incidents"
            >
              Incidents
            </NavLink>
            <NavLink
              className={({ isActive }) =>
                `min-h-11 rounded-md px-3 py-3 text-xs font-medium sm:min-h-0 sm:py-2 ${
                  isActive ? 'bg-accent-soft text-accent-hover' : 'text-muted hover:text-ink'
                }`
              }
              to="/security"
            >
              Security
            </NavLink>
            <NavLink
              className={({ isActive }) =>
                `min-h-11 rounded-md px-3 py-3 text-xs font-medium sm:min-h-0 sm:py-2 ${
                  isActive ? 'bg-accent-soft text-accent-hover' : 'text-muted hover:text-ink'
                }`
              }
              to="/demo"
            >
              Demo
            </NavLink>
          </div>
        </nav>

        <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">
          <Outlet />
        </main>
      </div>
    </SnapshotStreamProvider>
  );
}
