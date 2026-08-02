import { NavLink } from 'react-router';

import { Activity, FlaskConical, LayoutGrid, ShieldAlert, type LucideIcon } from '@/ui/icons';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  hint: string;
  end?: boolean;
}

/**
 * Every item is a real route.
 *
 * Incidents used to point at `/command#live-incidents` — an anchor into the
 * screen you were already on, which never rendered active and so read as a dead
 * button. It was a missing screen wearing a nav label, and the nav is where
 * that lie was visible.
 */
const ITEMS: NavItem[] = [
  { to: '/command', label: 'Command', icon: LayoutGrid, hint: 'Live decomposition and feed', end: true },
  { to: '/incidents', label: 'Incidents', icon: Activity, hint: 'Every durable decision' },
  { to: '/security', label: 'Security', icon: ShieldAlert, hint: 'Attack evidence and cohorts' },
  { to: '/demo', label: 'Demo', icon: FlaskConical, hint: 'Fire a scenario' },
];

export function CommandNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5 p-3">
      <p className="eyebrow font-sans px-2 pt-1 pb-2">Console</p>
      {ITEMS.map((item) => {
        const Icon = item.icon;
        return (
          <NavLink
            key={item.to}
            to={item.to}
            {...(item.end === true ? { end: true } : {})}
            {...(onNavigate ? { onClick: onNavigate } : {})}
            className={({ isActive }) =>
              `flex min-h-11 items-center gap-2.5 rounded-md px-2.5 text-xs font-bold transition-colors ${
                isActive
                  ? 'bg-accent-soft text-accent-hover'
                  : 'text-muted hover:text-ink hover:bg-hover'
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
                <span className="flex min-w-0 flex-col">
                  <span className="truncate">{item.label}</span>
                  {/* On the active pill the fill is accent-soft, where
                      `--faint` measures 3.97:1 and fails AA — the token is
                      tuned for the page and the rail, not for a tint on top of
                      them. The hint therefore takes the active colour and
                      keeps its hierarchy through size and weight instead. */}
                  <span
                    className={`truncate text-[0.66rem] font-normal ${
                      isActive ? 'text-accent-hover' : 'text-faint'
                    }`}
                  >
                    {item.hint}
                  </span>
                </span>
                {/* The active route is named, not only tinted. */}
                {isActive && <span className="sr-only">(current)</span>}
              </>
            )}
          </NavLink>
        );
      })}
    </nav>
  );
}
