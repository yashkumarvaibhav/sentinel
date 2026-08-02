import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';

import { Info } from '@/ui/icons';

/**
 * The `i` affordance that gets prose off the hero (`UI_REMEDIATION.md` C7).
 *
 * The command center was roughly 60% explanation on first paint, and the fix the
 * owner proposed — move it behind an `i` — is right. The constraint is that the
 * explanation must not become *less* available in the process, so this is
 * deliberately not the two things it is usually built as:
 *
 * - **not a `title=` attribute** — it never appears on touch, most screen
 *   readers do not announce it by default, and it cannot be styled or contain
 *   markup;
 * - **not a hover-only div** — unreachable by keyboard and by anyone who cannot
 *   hold a pointer still.
 *
 * It is a real `<button>` running the standard disclosure contract: Enter/Space
 * opens, Escape closes, and focus returns to the trigger so a keyboard user is
 * not dropped at the top of the document.
 */
export function InfoPopover({
  label,
  children,
  className = '',
}: {
  /** What the button announces, e.g. "About the decomposition". */
  label: string;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  const close = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.stopPropagation();
        close(true);
      }
    }

    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      // A click elsewhere is the user moving on: close, but leave focus where
      // they put it rather than yanking it back to the trigger.
      close(false);
    }

    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open, close]);

  return (
    <span className={`relative inline-flex ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={label}
        onClick={() => setOpen((wasOpen) => !wasOpen)}
        className="text-faint hover:text-accent hover:border-line-strong border-line grid size-11 shrink-0 place-items-center rounded-md border bg-transparent transition-colors sm:size-6 sm:rounded-sm"
      >
        <Info aria-hidden="true" className="size-4 sm:size-3.5" strokeWidth={2.25} />
      </button>

      {open && (
        <div
          ref={panelRef}
          id={panelId}
          role="note"
          className="border-line bg-raised text-body shadow absolute top-full left-0 z-30 mt-2 w-72 rounded-md border p-3 text-xs leading-relaxed"
        >
          {children}
        </div>
      )}
    </span>
  );
}
