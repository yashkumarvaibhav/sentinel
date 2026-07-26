import { useEffect, useState } from 'react';

import { DecompositionUnavailableError, fetchDecomposition } from '@/api/decomposition';
import type { DecompositionWindow } from '@/api/decomposition';
import { DecompositionChart } from '@/components/DecompositionChart';

const LEGEND = [
  { label: 'explained base', className: 'bg-base' },
  { label: 'explained by event', className: 'bg-event' },
  { label: 'unexplained residual', className: 'bg-residual' },
];

type Load =
  | { state: 'loading' }
  | { state: 'ok'; window: DecompositionWindow }
  | { state: 'unavailable'; detail: string }
  | { state: 'error'; detail: string };

export interface DecompositionPanelProps {
  service?: string;
  signal?: string;
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
  signal = 'ingress.requests',
}: DecompositionPanelProps) {
  const [load, setLoad] = useState<Load>({ state: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    fetchDecomposition({ service, signal }, controller.signal)
      .then((window) => setLoad({ state: 'ok', window }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof DecompositionUnavailableError) {
          setLoad({ state: 'unavailable', detail: error.message });
          return;
        }
        setLoad({ state: 'error', detail: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      controller.abort();
    };
  }, [service, signal]);

  const empty = load.state === 'ok' && load.window.count === 0;

  return (
    <section className="border-line bg-raised flex flex-col gap-4 rounded-xl border p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <h2 className="text-sm font-medium tracking-wide uppercase">Decomposition</h2>
        <div className="flex items-center gap-2">
          <code className="text-muted font-mono text-[11px]">
            {service} · {signal}
          </code>
          <span
            className="border-line text-muted rounded border px-2 py-0.5 text-[11px] tracking-wider uppercase"
            title="Frames are read from the platform's own store — no illustrative data is drawn here"
          >
            Real
          </span>
        </div>
      </div>

      {load.state === 'loading' && <p className="text-muted text-sm">Reading the window…</p>}

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
