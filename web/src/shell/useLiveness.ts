import { useEffect, useState } from 'react';

import { fetchHealth } from '@/api/platform';
import type { HealthReport } from '@/api/platform';

/**
 * What the connection indicator is allowed to claim.
 *
 * `degraded` is a distinct state from `down` on purpose: the gateway answering
 * 503 with a full component list is the platform successfully telling us
 * something is wrong, which is the opposite of not answering. Collapsing the
 * two would make a healthy monitoring system look identical to an absent one.
 */
export type Liveness = 'connecting' | 'live' | 'degraded' | 'down';

export interface LivenessState {
  status: Liveness;
  report: HealthReport | null;
  /** When the gateway last answered at all, successfully or not. */
  lastContact: Date | null;
  /** How this is being measured, so the UI never implies a stream it lacks. */
  transport: 'poll';
}

export const POLL_INTERVAL_MS = 10_000;

/**
 * Whether the platform is answering, measured by polling `/api/health`.
 *
 * **Deliberately polling, and deliberately labelled as such.** `ARCHITECTURE.md`
 * §2 puts every live stream on SSE, but no `/stream` endpoint exists yet — that
 * is a gateway slice, not a shell one. A dot that implied a live subscription
 * while a timer ticked behind it would be the UI telling its first lie, in the
 * component whose entire job is to say whether the platform is talking to us.
 * `transport` is on the returned state so the tooltip can say "polled every
 * 10s" rather than "live".
 */
export function useLiveness(intervalMs: number = POLL_INTERVAL_MS): LivenessState {
  const [state, setState] = useState<LivenessState>({
    status: 'connecting',
    report: null,
    lastContact: null,
    transport: 'poll',
  });

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const check = async () => {
      try {
        const report = await fetchHealth(controller.signal);
        if (cancelled) return;
        setState({
          status: report.status === 'ready' ? 'live' : 'degraded',
          report,
          lastContact: new Date(),
          transport: 'poll',
        });
      } catch {
        if (cancelled) return;
        // Keep the last report: "it was healthy 40 seconds ago and is now
        // unreachable" is more useful than an empty panel, and the status
        // field already says the platform is not answering.
        setState((previous) => ({ ...previous, status: 'down' }));
      }
    };

    void check();
    const timer = window.setInterval(() => void check(), intervalMs);

    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [intervalMs]);

  return state;
}
