import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router';

import { fetchIncidents } from '@/api/incidents';
import type { IncidentFeedItem, IncidentFeedResponse } from '@/contracts/types';
import { ObservationBanner } from '@/components/IncidentFeed';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { Chip, HonestyChip } from '@/ui/Chip';
import { RelativeTime } from '@/ui/RelativeTime';
import { SkeletonText } from '@/ui/Skeleton';
import { verdictIcon } from '@/ui/verdict';

const INCIDENT_RESOURCES = ['incidents'] as const;
const LOG_LIMIT = 200;

type Load =
  | { state: 'loading' }
  | { state: 'ready'; response: IncidentFeedResponse }
  | { state: 'error'; detail: string };

function words(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

const SEVERITY_TONE = {
  CRITICAL: 'offline',
  HIGH: 'offline',
  MEDIUM: 'degraded',
  LOW: 'advisory',
} as const;

const STATE_TONE = {
  OPEN: 'offline',
  MITIGATING: 'degraded',
  MONITORING: 'advisory',
  RESOLVED: 'healthy',
} as const;

/** Every filter is derived from what is actually in the window, never hardcoded. */
function distinct(items: IncidentFeedItem[], pick: (item: IncidentFeedItem) => string | null) {
  return [...new Set(items.map(pick).filter((value): value is string => value !== null))].sort();
}

/**
 * The incident log (`UIUX_SPEC.md` §3).
 *
 * This route did not exist. The nav's Incidents item pointed at
 * `/command#live-incidents` — an anchor into the screen you were already on —
 * so the item never rendered active and read as a dead button. It was a missing
 * screen wearing a nav label.
 *
 * The command center's feed answers "what is happening now" in a few rich
 * cards. This answers a different question — "find me the one I am thinking
 * of" — so it is a dense filterable table, which is the compact-density surface
 * the house system is built around. It is the same data through the same
 * reader; nothing here fetches differently.
 */
export function IncidentsPage() {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [query, setQuery] = useState('');
  const [verdict, setVerdict] = useState('');
  const [severity, setSeverity] = useState('');
  const controller = useRef<AbortController | null>(null);

  const refetch = useCallback((): Promise<void> => {
    const requestController = new AbortController();
    controller.current = requestController;
    return fetchIncidents(requestController.signal, LOG_LIMIT)
      .then((response) => {
        if (requestController.signal.aborted) return;
        setLoad(
          response.status === 'ready'
            ? { state: 'ready', response }
            : { state: 'error', detail: response.detail ?? 'live incident store unavailable' },
        );
      })
      .catch((error: unknown) => {
        if (requestController.signal.aborted) return;
        setLoad({ state: 'error', detail: error instanceof Error ? error.message : String(error) });
      });
  }, []);

  useSnapshotInvalidation(INCIDENT_RESOURCES, refetch);
  useEffect(() => {
    void refetch();
    return () => controller.current?.abort();
  }, [refetch]);

  const items = useMemo(
    () => (load.state === 'ready' ? load.response.incidents : []),
    [load],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return items.filter((item) => {
      if (verdict !== '' && (item.verdict_class ?? 'UNCLASSIFIED') !== verdict) return false;
      if (severity !== '' && item.severity !== severity) return false;
      if (needle === '') return true;
      const haystack = [
        item.incident_id,
        item.reason,
        item.explanation ?? '',
        item.origin_service ?? '',
        ...item.services,
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [items, query, verdict, severity]);

  const filtering = query.trim() !== '' || verdict !== '' || severity !== '';

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-col gap-2">
        <h1 className="font-serif">Incident log</h1>
        <p className="text-body max-w-2xl text-sm">
          Every durable decision the platform has recorded, newest first. The command center
          shows the latest few in full; this is the one to search.
        </p>
      </header>

      {load.state === 'ready' && <ObservationBanner observation={load.response.observation} />}

      <div className="flex flex-wrap items-end gap-3">
        <div className="flex min-w-56 flex-1 flex-col gap-1.5">
          <label htmlFor="incident-search" className="eyebrow font-sans">
            Search
          </label>
          <input
            id="incident-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="id, reason, service…"
            className="border-line bg-raised text-ink min-h-11 rounded-md border px-3 text-sm"
          />
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="incident-verdict" className="eyebrow font-sans">
            Verdict
          </label>
          <select
            id="incident-verdict"
            value={verdict}
            onChange={(event) => setVerdict(event.target.value)}
            className="border-line bg-raised text-ink min-h-11 rounded-md border px-3 text-sm"
          >
            <option value="">All verdicts</option>
            {distinct(items, (item) => item.verdict_class ?? 'UNCLASSIFIED').map((value) => (
              <option key={value} value={value}>
                {words(value)}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1.5">
          <label htmlFor="incident-severity" className="eyebrow font-sans">
            Severity
          </label>
          <select
            id="incident-severity"
            value={severity}
            onChange={(event) => setSeverity(event.target.value)}
            className="border-line bg-raised text-ink min-h-11 rounded-md border px-3 text-sm"
          >
            <option value="">All severities</option>
            {distinct(items, (item) => item.severity).map((value) => (
              <option key={value} value={value}>
                {words(value)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {load.state === 'loading' && (
        <SkeletonText lines={6} label="Reading the incident log…" />
      )}

      {load.state === 'error' && (
        <p className="text-bad text-sm" role="alert">
          Could not read the incident log: {load.detail}
        </p>
      )}

      {load.state === 'ready' && (
        <>
          <p className="text-muted text-xs" role="status">
            {filtering
              ? `${filtered.length} of ${items.length} recorded incidents match.`
              : `${items.length} recorded ${items.length === 1 ? 'incident' : 'incidents'}.`}
          </p>

          {items.length === 0 ? (
            // The refusal the feed makes, kept verbatim here: an empty log is a
            // statement about what was recorded, not about whether anything is
            // wrong. It is not replaced with a friendly illustration.
            <p className="border-line text-muted rounded-lg border border-dashed p-6 text-sm">
              Nothing has been recorded. An empty log means nothing has been recorded, not that
              nothing is wrong.
            </p>
          ) : filtered.length === 0 ? (
            <p className="border-line text-muted rounded-lg border border-dashed p-6 text-sm">
              No recorded incident matches these filters. {items.length} are in the window.
            </p>
          ) : (
            <div className="border-line overflow-x-auto rounded-lg border">
              <table className="w-full border-collapse text-left text-[0.83rem]">
                <caption className="sr-only">
                  Recorded incidents, newest first, with verdict, severity, state and origin.
                </caption>
                <thead>
                  <tr className="bg-sidebar">
                    {['Verdict', 'Severity', 'State', 'Origin', 'Reason', 'Source', 'Updated'].map(
                      (heading) => (
                        <th
                          key={heading}
                          scope="col"
                          className="text-faint border-line border-b px-3 py-2 text-[0.66rem] font-extrabold tracking-wider uppercase"
                        >
                          {heading}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((item) => {
                    const verdictValue = item.verdict_class ?? 'UNCLASSIFIED';
                    const VerdictIcon = verdictIcon(verdictValue);
                    return (
                      <tr key={item.incident_id} className="hover:bg-hover">
                        <td className="border-line text-ink border-b px-3 py-2 whitespace-nowrap">
                          <Link
                            to={`/incidents/${encodeURIComponent(item.incident_id)}`}
                            className="text-accent inline-flex items-center gap-1.5 font-bold underline decoration-transparent underline-offset-4 hover:decoration-current"
                          >
                            <VerdictIcon
                              aria-hidden="true"
                              className="size-3.5 shrink-0"
                              strokeWidth={2.25}
                            />
                            {words(verdictValue)}
                          </Link>
                        </td>
                        <td className="border-line border-b px-3 py-2">
                          <Chip tone={SEVERITY_TONE[item.severity]}>{words(item.severity)}</Chip>
                        </td>
                        <td className="border-line border-b px-3 py-2">
                          <Chip tone={STATE_TONE[item.state]}>{words(item.state)}</Chip>
                        </td>
                        <td className="border-line text-body border-b px-3 py-2 font-mono text-[0.72rem] whitespace-nowrap">
                          {item.origin_service ?? '—'}
                        </td>
                        <td className="border-line text-body border-b px-3 py-2">
                          <span className="line-clamp-2">{item.reason}</span>
                        </td>
                        <td className="border-line border-b px-3 py-2">
                          <HonestyChip kind={item.honesty} />
                        </td>
                        <td className="border-line text-muted border-b px-3 py-2 tabular-nums whitespace-nowrap">
                          <RelativeTime iso={item.updated_at} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
