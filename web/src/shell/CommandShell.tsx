import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Link, Outlet } from 'react-router';

import { AudienceBar } from '@/shell/AudienceBar';
import { CommandNav } from '@/shell/CommandNav';
import { ConnectionBanner } from '@/shell/ConnectionBanner';
import { IncidentAlarm } from '@/shell/IncidentAlarm';
import { ScenarioActivityBar } from '@/shell/ScenarioActivityBar';
import { TopBar } from '@/shell/TopBar';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';
import { BrandMark } from '@/ui/BrandMark';

function BrandBlock() {
  return (
    <Link to="/command" className="flex min-w-0 items-center gap-2.5">
      <span className="border-line bg-raised flex size-10 shrink-0 items-center justify-center rounded-sm border">
        <BrandMark className="size-8" />
      </span>
      <span className="text-ink truncate font-serif text-2xl font-medium tracking-tight">
        Sentinel
      </span>
    </Link>
  );
}

/**
 * The frame every screen renders inside.
 *
 * A persistent rail rather than a centred column. The content used to be pinned
 * to 1152px, which is a 55% gutter on a wide monitor — an editorial width worn
 * by a dashboard. A command center is the kit's compact-density case: the rail
 * carries the navigation so the working area keeps the full width, and the
 * screen stops looking like an article about a control room.
 *
 * The rail collapses to a drawer below `lg` rather than disappearing, because
 * the routes it lists are the only way to reach three of the four screens.
 */
export function CommandShell() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const openButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (drawerOpen) closeButtonRef.current?.focus();
  }, [drawerOpen]);

  function closeDrawer() {
    setDrawerOpen(false);
    openButtonRef.current?.focus();
  }

  // A drawer that can be tabbed out of behind its own backdrop is a trap of the
  // other kind: focus lands on controls the user cannot see.
  function onDrawerKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      closeDrawer();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusables = drawerRef.current?.querySelectorAll<HTMLElement>(
      "a[href], button:not([tabindex='-1'])",
    );
    if (focusables === undefined || focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <SnapshotStreamProvider>
      <div className="bg-page flex min-h-screen">
        {/* First stop on the tab order: a keyboard user should not have to walk
            the header to reach the incident that woke them up. */}
        <a
          href="#main"
          className="border-line bg-raised text-accent sr-only focus:not-sr-only focus:fixed focus:top-4 focus:left-4 focus:z-50 focus:rounded-md focus:border focus:px-4 focus:py-2.5 focus:text-sm focus:font-bold"
        >
          Skip to content
        </a>

        <aside className="border-line bg-sidebar sticky top-0 hidden h-screen w-64 shrink-0 overflow-y-auto border-r lg:block">
          <div className="border-line border-b px-5 py-5">
            <BrandBlock />
            <p className="text-muted mt-2 text-xs">Context-aware observability console</p>
          </div>
          <CommandNav />
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar onOpenNav={() => setDrawerOpen(true)} navOpen={drawerOpen} openNavRef={openButtonRef} />

          <AudienceBar />
          <ScenarioActivityBar />
          <ConnectionBanner />
          <IncidentAlarm />

          {/* `overflow-x-clip` rather than `hidden`: sticky positioning inside
              still works, and the page body stops scrolling sideways when a
              graph or an unbreakable identifier is wider than a phone. */}
          <main
            id="main"
            tabIndex={-1}
            className="min-w-0 flex-1 overflow-x-clip px-4 py-6 focus:outline-none sm:px-6 lg:px-8"
          >
            <Outlet />
          </main>
        </div>

        {drawerOpen && (
          <div className="fixed inset-0 z-40 lg:hidden">
            <button
              type="button"
              aria-label="Close navigation"
              tabIndex={-1}
              onClick={closeDrawer}
              className="absolute inset-0 bg-black/40"
            />
            <div
              ref={drawerRef}
              role="dialog"
              aria-modal="true"
              aria-label="Navigation"
              onKeyDown={onDrawerKeyDown}
              className="border-line bg-sidebar absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col overflow-y-auto border-r"
            >
              <div className="border-line flex items-center justify-between gap-2 border-b px-4 py-3">
                <BrandBlock />
                <button
                  ref={closeButtonRef}
                  type="button"
                  onClick={closeDrawer}
                  aria-label="Close navigation"
                  className="border-line text-ink hover:bg-hover flex size-11 shrink-0 items-center justify-center rounded-md border"
                >
                  <svg
                    aria-hidden="true"
                    width="18"
                    height="18"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                  >
                    <path d="M6 6l12 12M18 6L6 18" />
                  </svg>
                </button>
              </div>
              <CommandNav onNavigate={closeDrawer} />
            </div>
          </div>
        )}
      </div>
    </SnapshotStreamProvider>
  );
}
