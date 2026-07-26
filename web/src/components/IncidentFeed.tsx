import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';

import { fetchIncidents } from '@/api/incidents';
import type { IncidentFeedItem, IncidentFeedResponse } from '@/contracts/types';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

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

function timeAgo(timestamp: string): string {
  return new Intl.DateTimeFormat('en', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  }).format(new Date(timestamp));
}

function IncidentCard({ item }: { item: IncidentFeedItem }) {
  const verdict = item.verdict_class ?? 'UNCLASSIFIED';
  return (
    <article
      aria-label={`${label(verdict)} incident ${item.incident_id}`}
      className={`border-line flex flex-col gap-4 rounded-lg border p-4 ${
        item.muted ? 'bg-sidebar opacity-75' : 'bg-raised'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <strong className="bg-accent-soft text-accent rounded px-2 py-1 text-xs tracking-wide uppercase">
          {label(verdict)}
        </strong>
        <span className="border-line rounded border px-2 py-1 text-[10px] tracking-wider">
          {item.honesty}
        </span>
        <span className="text-muted text-xs">{label(item.severity)}</span>
        <span className="text-muted ml-auto text-xs">{timeAgo(item.updated_at)} UTC</span>
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

export function IncidentFeed() {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
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
    <section className="flex flex-col gap-4" aria-labelledby="live-incidents">
      <div>
        <h2 id="live-incidents" className="text-sm font-medium tracking-wide uppercase">
          Live incidents
        </h2>
        <p className="text-muted mt-1 text-xs">
          Latest durable decisions. Stream events only ask this view to reread the REST snapshot.
        </p>
      </div>

      {load.state === 'loading' && (
        <p className="text-muted text-sm" role="status">
          Reading live incidents…
        </p>
      )}
      {load.state === 'error' && (
        <p className="text-bad text-sm" role="alert">
          Live incident snapshot unavailable: {load.detail}
        </p>
      )}
      {load.state === 'ready' && (
        <div
          role="feed"
          aria-live="polite"
          aria-busy="false"
          aria-label="Latest live incidents"
          className="grid gap-3"
        >
          {load.response.incidents.length === 0 ? (
            <p className="border-line bg-sidebar text-muted rounded-lg border p-4 text-sm">
              No live incidents have been persisted. This is not a zero-risk claim: no deployed
              live decision producer has written a current incident.
            </p>
          ) : (
            load.response.incidents.map((item) => (
              <IncidentCard item={item} key={item.incident_id} />
            ))
          )}
        </div>
      )}
    </section>
  );
}
