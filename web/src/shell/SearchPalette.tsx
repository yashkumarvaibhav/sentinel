import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';

import { fetchIncidents } from '@/api/incidents';
import type { IncidentFeedItem } from '@/contracts/types';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { Activity, FlaskConical, LayoutGrid, Search, ShieldAlert, type LucideIcon } from '@/ui/icons';
import { verdictIcon } from '@/ui/verdict';

const INCIDENT_RESOURCES = ['incidents'] as const;

interface Hit {
  id: string;
  label: string;
  detail: string;
  to: string;
  icon: LucideIcon;
}

const ROUTES: Hit[] = [
  { id: 'r-command', label: 'Command center', detail: 'Live decomposition and feed', to: '/command', icon: LayoutGrid },
  { id: 'r-incidents', label: 'Incident log', detail: 'Every durable decision', to: '/incidents', icon: Activity },
  { id: 'r-security', label: 'Security evidence', detail: 'Attack evidence and cohorts', to: '/security', icon: ShieldAlert },
  { id: 'r-demo', label: 'Demo launcher', detail: 'Fire a scenario', to: '/demo', icon: FlaskConical },
];

function words(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

/**
 * Global search (`UIUX_SPEC.md` §3, "global search (⌘K)").
 *
 * It searches the two things an operator actually looks for by name: a screen,
 * and an incident. Incidents come from the same reader every other surface
 * uses — this is not a second index that can disagree with the feed.
 *
 * Implemented as a real dialog with a listbox rather than a filtered dropdown,
 * so the arrow keys, the active option and the labelling all mean what
 * assistive technology expects them to mean.
 */
export function SearchPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const [incidents, setIncidents] = useState<IncidentFeedItem[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();

  const read = useCallback((): Promise<void> => {
    const controller = new AbortController();
    return fetchIncidents(controller.signal, 200)
      .then((response) => {
        if (response.status === 'ready') setIncidents(response.incidents);
      })
      .catch(() => {
        // The connection banner already reports an unreachable gateway.
      });
  }, []);

  useSnapshotInvalidation(INCIDENT_RESOURCES, read);
  useEffect(() => {
    void read();
  }, [read]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((wasOpen) => !wasOpen);
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
    else {
      setQuery('');
      setCursor(0);
    }
  }, [open]);

  const hits = useMemo<Hit[]>(() => {
    const needle = query.trim().toLowerCase();
    const routes = ROUTES.filter(
      (route) =>
        needle === '' ||
        `${route.label} ${route.detail}`.toLowerCase().includes(needle),
    );
    const matched = incidents
      .filter((item) => {
        if (needle === '') return false;
        return [item.incident_id, item.reason, item.origin_service ?? '', ...item.services]
          .join(' ')
          .toLowerCase()
          .includes(needle);
      })
      .slice(0, 8)
      .map<Hit>((item) => ({
        id: item.incident_id,
        label: `${words(item.verdict_class ?? 'unclassified')} · ${item.origin_service ?? 'no origin'}`,
        detail: item.reason,
        to: `/incidents/${encodeURIComponent(item.incident_id)}`,
        icon: verdictIcon(item.verdict_class ?? 'UNCLASSIFIED'),
      }));
    return [...routes, ...matched];
  }, [query, incidents]);

  function close(returnFocus: boolean) {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  }

  function go(hit: Hit | undefined) {
    if (hit === undefined) return;
    close(false);
    void navigate(hit.to);
  }

  function onInputKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close(true);
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setCursor((index) => (hits.length === 0 ? 0 : (index + 1) % hits.length));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setCursor((index) => (hits.length === 0 ? 0 : (index - 1 + hits.length) % hits.length));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      go(hits[cursor]);
    }
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        className="border-line bg-raised text-muted hover:bg-hover flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-md border px-3 text-sm transition-colors sm:min-h-0 sm:max-w-md sm:py-2"
      >
        <Search aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
        <span className="truncate">Search the console</span>
        <kbd className="border-line text-faint ml-auto hidden shrink-0 rounded border px-1.5 py-0.5 font-mono text-[0.66rem] sm:inline">
          ⌘K
        </kbd>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-start justify-center p-4 sm:pt-24">
          <button
            type="button"
            aria-label="Close search"
            tabIndex={-1}
            onClick={() => close(true)}
            className="absolute inset-0 bg-black/40"
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Search the console"
            className="border-line bg-raised shadow-lg relative flex w-full max-w-xl flex-col rounded-lg border"
          >
            <div className="border-line flex items-center gap-2 border-b px-3 py-1">
              <Search aria-hidden="true" className="text-faint size-4 shrink-0" strokeWidth={2} />
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setCursor(0);
                }}
                onKeyDown={onInputKeyDown}
                placeholder="Screens, incident ids, services, reasons…"
                aria-label="Search the console"
                aria-controls="search-hits"
                aria-activedescendant={hits[cursor] ? `hit-${hits[cursor].id}` : undefined}
                className="text-ink min-h-11 w-full min-w-0 bg-transparent text-sm outline-none"
              />
            </div>

            <ul id="search-hits" role="listbox" aria-label="Results" className="max-h-80 overflow-y-auto overflow-x-hidden p-1">
              {hits.length === 0 ? (
                <li className="text-muted px-3 py-4 text-sm">
                  Nothing matches “{query}”. Incidents are searched by id, service and reason.
                </li>
              ) : (
                hits.map((hit, index) => {
                  const Icon = hit.icon;
                  return (
                    <li key={hit.id} id={`hit-${hit.id}`} role="option" aria-selected={index === cursor}>
                      <button
                        type="button"
                        onClick={() => go(hit)}
                        onMouseEnter={() => setCursor(index)}
                        className={`flex w-full min-w-0 items-center gap-2.5 rounded-md px-3 py-2 text-left transition-colors ${
                          index === cursor ? 'bg-accent-soft text-ink' : 'text-body hover:bg-hover'
                        }`}
                      >
                        <Icon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-bold">{hit.label}</span>
                          <span className="text-faint block truncate text-xs font-normal">
                            {hit.detail}
                          </span>
                        </span>
                      </button>
                    </li>
                  );
                })
              )}
            </ul>
          </div>
        </div>
      )}
    </>
  );
}
