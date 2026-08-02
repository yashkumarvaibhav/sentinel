import { useCallback, useEffect, useRef, useState } from 'react';

import { DecompositionUnavailableError, fetchDecomposition } from '@/api/decomposition';
import type { DecompositionWindow } from '@/api/decomposition';
import { fetchScenarioActivity } from '@/api/activity';
import type { ScenarioActivity } from '@/api/activity';
import { DecompositionChart } from '@/components/DecompositionChart';
import { HonestyChip } from '@/ui/Chip';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { Skeleton, SkeletonText } from '@/ui/Skeleton';

const LEGEND = [
  { label: 'explained base', className: 'bg-decomp-base' },
  { label: 'explained by event', className: 'bg-event' },
  { label: 'unexplained residual', className: 'bg-residual' },
];

type Load =
  | { state: 'loading' }
  | { state: 'ok'; window: DecompositionWindow }
  | { state: 'unavailable'; detail: string }
  | { state: 'error'; detail: string };

const DECOMPOSITION_RESOURCES = ['decomposition'] as const;

export interface DecompositionPanelProps {
  service?: string;
  signal?: string;
  className?: string;
}

/**
 * The hero panel, and the four states it must tell apart.
 *
 * The distinction that matters most is **empty versus zero**. A window with no
 * frames means nothing was recorded — which is what the live site reports today,
 * because `decomp_frames` is only written by a capture replay or a testbed run.
 * Drawing that as a flat line at zero would be the chart inventing a
 * measurement, in the exact place this product asks to be trusted. So an empty
 * window says so in words and draws nothing at all.
 *
 * `unavailable` is likewise kept apart from `error`: the gateway answering 503
 * is it telling us it has no store attached, which sends a reader somewhere
 * different from a failed request.
 */
export function DecompositionPanel({
  service = 'frontend',
  // What the platform actually stores. The panel asked for
  // `ingress.requests` - the raw span signal the rate is derived FROM - and so
  // read an empty window forever while the frames sat under `request_rate`.
  signal = 'request_rate',
  className = '',
}: DecompositionPanelProps) {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [activity, setActivity] = useState<ScenarioActivity | null>(null);
  const controller = useRef<AbortController | null>(null);

  const refetch = useCallback((): Promise<void> => {
    controller.current?.abort();
    const requestController = new AbortController();
    controller.current = requestController;
    const replayWindow =
      activity?.mode === 'REPLAY' &&
      activity.evidence_start_at !== null &&
      activity.evidence_cursor_at !== null
        ? {
            start: new Date(activity.evidence_start_at),
            end: new Date(activity.evidence_cursor_at),
          }
        : {};
    return fetchDecomposition({ service, signal, ...replayWindow }, requestController.signal)
      .then((window) => setLoad({ state: 'ok', window }))
      .catch((error: unknown) => {
        if (requestController.signal.aborted) return;
        if (error instanceof DecompositionUnavailableError) {
          setLoad({ state: 'unavailable', detail: error.message });
          return;
        }
        setLoad({ state: 'error', detail: error instanceof Error ? error.message : String(error) });
      });
  }, [activity, service, signal]);

  useEffect(() => {
    const controller = new AbortController();
    const read = () =>
      fetchScenarioActivity(controller.signal)
        .then(setActivity)
        .catch(() => undefined);
    void read();
    const timer = window.setInterval(() => void read(), activity?.in_flight === true ? 2_000 : 15_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [activity?.in_flight]);

  useSnapshotInvalidation(DECOMPOSITION_RESOURCES, refetch);
  useEffect(() => {
    void refetch();
    return () => {
      controller.current?.abort();
    };
  }, [refetch]);

  const empty = load.state === 'ok' && load.window.count === 0;

  return (
    <section
      className={`border-line bg-raised flex min-w-0 flex-col gap-4 rounded-lg border p-5 ${className}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <h2 className="font-serif text-base">Decomposition</h2>
        <div className="flex items-center gap-2">
          <code className="text-muted font-mono text-[11px]">
            {service} · {signal}
          </code>
          <HonestyChip kind="REAL" />
        </div>
      </div>

      {load.state === 'loading' && (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-48 w-full" />
          <SkeletonText lines={1} label="Reading the decomposition window…" />
        </div>
      )}

      {load.state === 'unavailable' && (
        <p className="text-warn text-sm" role="status">
          The decomposition store is not attached to this gateway. {load.detail}
        </p>
      )}

      {load.state === 'error' && (
        <p className="text-bad text-sm" role="alert">
          Could not read the decomposition: {load.detail}
        </p>
      )}

      {empty && (
        <p className="text-muted text-sm" role="status">
          No frames recorded in this window. Nothing has been decomposed here yet — which is not
          the same as a surge of zero, so nothing is drawn.
        </p>
      )}

      {load.state === 'ok' && load.window.count > 0 && (
        <>
          {(() => {
            const latest = load.window.frames.at(-1);
            if (latest === undefined) return null;
            return (
            <dl
              className="grid grid-cols-2 gap-2 sm:grid-cols-4"
              aria-label="Latest decomposition values"
            >
                {[
                  ['Observed', latest.observed],
                  ['Expected base', latest.explained_base],
                  ['Event lift', latest.explained_event],
                  ['Residual', latest.residual],
                ].map(([label, value]) => (
                  <div className="border-line bg-sidebar rounded-md border px-3 py-2" key={String(label)}>
                    <dt className="text-faint text-[10px] tracking-wide uppercase">{label}</dt>
                    <dd className="text-ink mt-1 text-lg font-bold tabular-nums">
                      {(value as number).toFixed(2)}
                    </dd>
                  </div>
                ))}
              </dl>
            );
          })()}
          <DecompositionChart frames={load.window.frames} />

          {load.window.truncated && (
            <p className="text-warn text-xs" role="status">
              Showing the first {load.window.limit} frames of a longer window. Narrow the range to
              see all of it.
            </p>
          )}

          <ul className="text-muted flex flex-wrap gap-x-5 gap-y-2 text-xs">
            {LEGEND.map((item) => (
              <li key={item.label} className="flex items-center gap-2">
                <span className={`inline-block size-2.5 rounded-full ${item.className}`} />
                {item.label}
              </li>
            ))}
            <li className="flex items-center gap-2">
              <span className="bg-muted inline-block h-px w-4" />
              observed
            </li>
          </ul>
        </>
      )}
    </section>
  );
}
