import { NavLink } from 'react-router';

import { useIncidentCounts } from '@/shell/useIncidentCounts';

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
  { to: '/command', label: 'Command', icon: LayoutGrid, hint: 'Decomposition and feed', end: true },
  { to: '/incidents', label: 'Incidents', icon: Activity, hint: 'Durable decisions' },
  { to: '/security', label: 'Security', icon: ShieldAlert, hint: 'Attack evidence' },
  { to: '/demo', label: 'Demo', icon: FlaskConical, hint: 'Fire a scenario' },
];

export function CommandNav({ onNavigate }: { onNavigate?: () => void }) {
  const { total, unresolved } = useIncidentCounts();

  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5 p-3">
      <p className="text-faint px-3 pt-1 pb-2 text-[11px] font-bold tracking-[0.14em] uppercase">Console</p>
      {ITEMS.map((item) => {
        const Icon = item.icon;
        return (
          <NavLink
            key={item.to}
            to={item.to}
            {...(item.end === true ? { end: true } : {})}
            {...(onNavigate ? { onClick: onNavigate } : {})}
            className={({ isActive }) =>
              `flex min-h-11 items-center gap-3 rounded-md border px-3 py-2 text-sm transition-colors ${
                isActive
                  ? 'border-line-strong bg-accent-soft font-semibold text-ink'
                  : 'text-body border-transparent hover:bg-hover hover:text-ink'
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  aria-hidden="true"
                  className={`size-[18px] shrink-0 ${isActive ? 'text-accent' : ''}`}
                  strokeWidth={2}
                />
                <span className="flex min-w-0 flex-col">
                  <span className="truncate">{item.label}</span>
                  {/* On the active pill the fill is accent-soft, where
                      `--faint` measures 3.97:1 and fails AA — the token is
                      tuned for the page and the rail, not for a tint on top of
                      them. The hint therefore takes the active colour and
                      keeps its hierarchy through size and weight instead. */}
                  <span
                    className={`truncate text-xs font-normal ${
                      isActive ? 'text-body' : 'text-faint'
                    }`}
                  >
                    {item.hint}
                  </span>
                </span>
                {/* A count that moves without a reload is the cheapest signal
                    that this console is live. It is absent rather than zero
                    when nothing has been read - see `useIncidentCounts`. */}
                {item.to === '/incidents' && total !== null && (
                  <span
                    className={`ml-auto shrink-0 rounded-full px-2 py-0.5 text-xs font-bold tabular-nums ${
                      unresolved !== null && unresolved > 0
                        ? 'text-danger border-danger-line border'
                        : 'text-faint border-line border'
                    }`}
                  >
                    {unresolved !== null && unresolved > 0 ? unresolved : total}
                    <span className="sr-only">
                      {unresolved !== null && unresolved > 0
                        ? ' unresolved incidents'
                        : ' recorded incidents'}
                    </span>
                  </span>
                )}
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
