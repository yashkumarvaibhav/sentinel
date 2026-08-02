import { useTheme } from '@/shell/useTheme';
import { Moon, Sun } from '@/ui/icons';

/**
 * The theme control, as the house recipe draws it (`~/brand-kit/patterns.md`):
 * a 44×44 icon button showing **the action it offers**, not the state you are
 * in — sun while dark, moon while light.
 *
 * This replaced a three-option menu (decision #143 supersedes #142). The menu
 * was built to preserve `system` as a state you could return to; the owner
 * chose the toggle that the other products in this family ship, and consistency
 * across them is worth more than a third stop almost nobody walks back to.
 * `system` is not gone — it is still what an operator who has never chosen
 * gets, and the OS flipping at sunset still moves the page and this icon with
 * it. It simply stops being somewhere the button can cycle to.
 */
export function ThemeToggle() {
  const { resolved, toggle } = useTheme();
  const label = resolved === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className="border-line text-ink hover:bg-hover flex size-11 shrink-0 items-center justify-center rounded-md border transition-colors"
    >
      {resolved === 'dark' ? (
        <Sun aria-hidden="true" className="size-[18px]" strokeWidth={2} />
      ) : (
        <Moon aria-hidden="true" className="size-[18px]" strokeWidth={2} />
      )}
    </button>
  );
}
