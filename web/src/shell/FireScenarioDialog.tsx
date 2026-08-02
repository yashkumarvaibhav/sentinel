import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Link } from 'react-router';

import { DemoLauncher } from '@/components/DemoLauncher';
import { BUTTON_BASE, BUTTON_VARIANT } from '@/ui/buttonStyles';
import { Zap } from '@/ui/icons';

/**
 * Firing a scenario from wherever you are.
 *
 * This used to be a link to `/demo`, which meant leaving the screen you wanted
 * to watch in order to start the thing you wanted to watch it react to. The
 * launcher is a small enough surface to bring to the operator instead.
 *
 * It mounts the same `DemoLauncher` the route does — not a copy of it. A second
 * set of scenario buttons that could drift from the real ones is exactly the
 * kind of thing that demos wrong at the worst moment.
 */
export function FireScenarioDialog() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) closeRef.current?.focus();
  }, [open]);

  function close() {
    setOpen(false);
    triggerRef.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
      "a[href], button:not([disabled]):not([tabindex='-1']), input, select",
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
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} shrink-0`}
      >
        <Zap aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
        <span className="hidden sm:inline">Fire a scenario</span>
        <span className="sm:hidden">Fire</span>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:pt-16">
          <button
            type="button"
            aria-label="Close"
            tabIndex={-1}
            onClick={close}
            className="fixed inset-0 bg-black/40"
          />
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="fire-scenario-title"
            onKeyDown={onKeyDown}
            className="border-line bg-page relative w-full max-w-3xl rounded-lg border p-5 shadow-lg"
          >
            <div className="mb-4 flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h2 id="fire-scenario-title" className="font-serif text-xl">
                  Fire a scenario
                </h2>
                <p className="text-muted mt-1 text-xs">
                  Watch the command center react without leaving it.{' '}
                  <Link to="/demo" onClick={close} className="text-accent font-bold underline underline-offset-4">
                    Open the full launcher
                  </Link>
                </p>
              </div>
              <button
                ref={closeRef}
                type="button"
                onClick={close}
                aria-label="Close"
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

            <DemoLauncher />
          </div>
        </div>
      )}
    </>
  );
}
