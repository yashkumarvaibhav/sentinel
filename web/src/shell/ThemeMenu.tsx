import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { useTheme, type ThemePreference } from '@/shell/useTheme';
import { Monitor, Moon, Sun, type LucideIcon } from '@/ui/icons';

interface Choice {
  value: ThemePreference;
  label: string;
  icon: LucideIcon;
  hint: string;
}

/** Named separately so it can be the fallback without an unchecked index. */
const SYSTEM_CHOICE: Choice = {
  value: 'system',
  label: 'System',
  icon: Monitor,
  hint: 'Follow this device',
};

const CHOICES: Choice[] = [
  { value: 'light', label: 'Light', icon: Sun, hint: 'Always light' },
  { value: 'dark', label: 'Dark', icon: Moon, hint: 'Always dark' },
  SYSTEM_CHOICE,
];

/**
 * The theme control (`UI_REMEDIATION.md` C4).
 *
 * It was a three-state cycle behind one unlabelled button captioned with the
 * state you were already in — so it read as a label rather than a control, and
 * reaching a specific theme meant clicking an unknown number of times.
 *
 * A menu rather than the house kit's two-state toggle, because `system` is kept
 * (BUILD_STATE decision #142) and three states cannot honestly be one
 * affordance showing "the action offered". Every option is named and the
 * current one is marked, so nothing has to be inferred and no state is
 * reachable only by cycling past the others.
 */
export function ThemeMenu() {
  const { preference, setPreference } = useTheme();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const current = CHOICES.find((choice) => choice.value === preference) ?? SYSTEM_CHOICE;
  const CurrentIcon = current.icon;

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
        return;
      }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      const items = Array.from(
        menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]') ?? [],
      );
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'ArrowDown' ? index + 1 : index - 1;
      items[(next + items.length) % items.length]?.focus();
    }

    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close(false);
    }

    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open, close]);

  // Opening with the keyboard should land on the menu, not leave focus behind.
  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
  }, [open]);

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Theme: ${current.label}. Change theme.`}
        onClick={() => setOpen((wasOpen) => !wasOpen)}
        className="border-line bg-raised text-ink hover:bg-hover hover:border-line-strong inline-flex min-h-11 items-center gap-2 rounded-md border px-3 text-xs font-bold transition-colors sm:min-h-0 sm:py-2"
      >
        <CurrentIcon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
        <span className="hidden sm:inline">{current.label}</span>
      </button>

      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label="Theme"
          className="border-line bg-raised shadow absolute right-0 z-30 mt-2 w-52 rounded-md border p-1"
        >
          {CHOICES.map((choice) => {
            const Icon = choice.icon;
            const checked = choice.value === preference;
            return (
              <button
                key={choice.value}
                type="button"
                role="menuitemradio"
                aria-checked={checked}
                onClick={() => {
                  setPreference(choice.value);
                  close(true);
                }}
                className={`flex min-h-11 w-full items-center gap-2 rounded-sm px-2 text-left text-xs font-bold transition-colors sm:min-h-9 ${
                  checked ? 'bg-accent-soft text-accent-hover' : 'text-body hover:bg-hover'
                }`}
              >
                <Icon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
                <span className="flex-1">{choice.label}</span>
                <span className="text-faint text-[0.66rem] font-normal whitespace-nowrap">{choice.hint}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
