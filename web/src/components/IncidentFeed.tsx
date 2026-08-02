import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';

import { fetchIncidents } from '@/api/incidents';
import type {
  IncidentFeedItem,
  IncidentFeedResponse,
  ObservationFreshness,
} from '@/contracts/types';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { HonestyChip } from '@/ui/Chip';
import { RelativeTime } from '@/ui/RelativeTime';
import { SkeletonText } from '@/ui/Skeleton';
import { verdictIcon } from '@/ui/verdict';

type Load =
  | { state: 'loading' }
  | { state: 'ready'; response: IncidentFeedResponse }
  | { state: 'error'; detail: string };

const INCIDENT_RESOURCES = ['incidents'] as const;

function label(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

function value(measured: number): string {
  return Number.isInteger(measured) ? measured.toLocaleString() : measured.toPrecision(3);
}

function IncidentCard({ item, fresh }: { item: IncidentFeedItem; fresh: boolean }) {
  const verdict = item.verdict_class ?? 'UNCLASSIFIED';
  const VerdictIcon = verdictIcon(verdict);
  return (
    <article
      aria-label={`${label(verdict)} incident ${item.incident_id}`}
      className={`flex flex-col gap-4 rounded-lg border p-4 transition-colors duration-500 ${
        item.muted ? 'bg-sidebar border-line opacity-75' : 'bg-raised border-line'
      } ${
        // What just arrived is ringed for a few seconds. On a screen left up on
        // a wall, the question is never "what is here" but "what changed", and
        // a card that appears silently among seven identical ones answers the
        // wrong one.
        fresh ? 'ring-accent border-line-strong ring-2' : ''
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <strong className="bg-accent-soft text-ink inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs tracking-wide uppercase">
          <VerdictIcon aria-hidden="true" className="size-3.5 shrink-0" strokeWidth={2.25} />
          {label(verdict)}
        </strong>
        <HonestyChip kind={item.honesty} />
        <span className="text-muted text-xs">{label(item.severity)}</span>
        <RelativeTime iso={item.updated_at} className="text-muted ml-auto text-xs" />
      </div>

      <div>
        {item.explanation !== null && (
          <p className="text-muted mb-1 text-xs font-medium">{item.explanation}</p>
        )}
        <p className="text-body text-sm leading-6">{item.reason}</p>
      </div>

      {item.evidence.length > 0 ? (
        <dl className="grid gap-2 sm:grid-cols-2">
          {item.evidence.map((evidence) => (
            <div className="border-line bg-sidebar rounded border p-3" key={evidence.feature}>
              <dt className="font-mono text-[11px]">{evidence.feature}</dt>
              <dd className="text-body mt-1 text-xs">
                {value(evidence.value)} vs {value(evidence.baseline)} baseline ·{' '}
                {label(evidence.direction)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="text-muted text-xs">No compact evidence values were attached.</p>
      )}

      <div className="border-line grid gap-2 border-t pt-3 text-xs sm:grid-cols-2">
        <div>
          <span className="text-muted">Action state · </span>
          <strong>{label(item.action.decision_action)}</strong>
          <p className="text-muted mt-1">{item.action.detail}</p>
        </div>
        <div>
          <span className="text-muted">Confidence · </span>
          <strong>
            {item.confidence.status === 'calibrated' && item.confidence.value !== null
              ? `${Math.round(item.confidence.value * 100)}% calibrated`
              : 'Insufficient confidence'}
          </strong>
          <p className="text-muted mt-1">{item.confidence.note}</p>
        </div>
      </div>
      <Link
        className="text-accent w-fit text-xs font-medium underline decoration-transparent underline-offset-4 hover:decoration-current"
        to={`/incidents/${encodeURIComponent(item.incident_id)}`}
      >
        Open evidence proof
      </Link>
    </article>
  );
}

function formatAge(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

/**
 * Whether anything is being measured at all. This is deliberately separate
 * from the cards: an incident can only ever be the last thing measured, and a
 * stopped producer must never let a stale card read as a calm mesh.
 */
export function ObservationBanner({ observation }: { observation: ObservationFreshness }) {
  if (observation.status === 'WATCHING') {
    return (
      <p className="text-muted text-xs" data-testid="observation" role="status">
        <span className="text-ok">●</span> {observation.note}
        {observation.age_seconds !== null && observation.age_seconds !== undefined
          ? ` Last judged ${formatAge(observation.age_seconds)} ago.`
          : ''}
      </p>
    );
  }
  const never = observation.status === 'NEVER';
  return (
    <p
      className="border-warn/40 bg-warn/10 text-warn rounded-lg border p-3 text-sm"
      data-testid="observation"
      role="status"
    >
      <strong className="font-semibold">
        {never ? 'Never observed' : 'Not currently watching'}
      </strong>{' '}
      — {observation.note}
      {!never && observation.age_seconds !== null && observation.age_seconds !== undefined
        ? ` Last judged ${formatAge(observation.age_seconds)} ago; expected within ${formatAge(
            observation.expected_within_seconds,
          )}.`
        : ''}
    </p>
  );
}

const FRESH_FOR_MS = 12_000;

export function IncidentFeed({ className = '' }: { className?: string } = {}) {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  const seen = useRef<Set<string> | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const refetchQueued = useRef(false);

  const refetch = useCallback((): Promise<void> => {
    if (inFlight.current !== null) {
      // A reconnect or invalidation that races an older request must get a
      // trailing read. Sharing only the old promise could miss the write that
      // caused the reconnect/invalidation.
      refetchQueued.current = true;
      return inFlight.current;
    }
    const requestController = new AbortController();
    controller.current = requestController;
    const request = fetchIncidents(requestController.signal)
      .then((response) => {
        if (!requestController.signal.aborted) {
          if (response.status === 'ready') {
            // The first read of a session is history, not news, so it seeds
            // without highlighting - the same rule the alarm follows.
            const ids = response.incidents.map((incident) => incident.incident_id);
            if (seen.current === null) {
              seen.current = new Set(ids);
            } else {
              const arrived = ids.filter((id) => !seen.current?.has(id));
              for (const id of ids) seen.current.add(id);
              if (arrived.length > 0) {
                setFresh((current) => new Set([...current, ...arrived]));
                window.setTimeout(() => {
                  setFresh((current) => {
                    const next = new Set(current);
                    for (const id of arrived) next.delete(id);
                    return next;
                  });
                }, FRESH_FOR_MS);
              }
            }
          }
          setLoad(
            response.status === 'ready'
              ? { state: 'ready', response }
              : { state: 'error', detail: response.detail ?? 'live incident store unavailable' },
          );
        }
      })
      .catch((error: unknown) => {
        if (!requestController.signal.aborted) {
          setLoad({
            state: 'error',
            detail: error instanceof Error ? error.message : String(error),
          });
        }
      })
      .finally(() => {
        inFlight.current = null;
        controller.current = null;
        if (refetchQueued.current && !requestController.signal.aborted) {
          refetchQueued.current = false;
          void refetch();
        }
      });
    inFlight.current = request;
    return request;
  }, []);

  useSnapshotInvalidation(INCIDENT_RESOURCES, refetch);
  useEffect(() => {
    void refetch();
    return () => controller.current?.abort();
  }, [refetch]);

  return (
    <section className={`flex min-w-0 flex-col gap-3 ${className}`} aria-labelledby="live-incidents">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="live-incidents" className="font-serif text-base">
          Live incidents
        </h2>
        {/* A count that moves is the cheapest signal that this is a live view
            and not a report someone generated earlier. */}
        {load.state === 'ready' && (
          <span className="text-faint text-xs tabular-nums">
            {load.response.incidents.length} recorded
          </span>
        )}
      </div>

      {load.state === 'loading' && (
        <div className="grid gap-3">
          <SkeletonText lines={4} label="Reading live incidents…" />
          <SkeletonText lines={4} label="" />
        </div>
      )}
      {load.state === 'error' && (
        <p className="text-bad text-sm" role="alert">
          Live incident snapshot unavailable: {load.detail}
        </p>
      )}
      {load.state === 'ready' && <ObservationBanner observation={load.response.observation} />}
      {load.state === 'ready' && load.response.incidents.length === 0 && (
        <p className="border-line bg-sidebar text-muted rounded-lg border p-4 text-sm">
          No live incidents have been persisted. This is not a zero-risk claim: an empty feed
          means nothing has been recorded, not that nothing is wrong.
        </p>
      )}
      {load.state === 'ready' && load.response.incidents.length > 0 && (
        <div
          role="feed"
          aria-live="polite"
          aria-busy="false"
          aria-label="Latest live incidents"
          className="grid max-h-[42rem] gap-3 overflow-y-auto pr-1"
        >
          {load.response.incidents.map((item) => (
            <IncidentCard
              item={item}
              key={item.incident_id}
              fresh={fresh.has(item.incident_id)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
