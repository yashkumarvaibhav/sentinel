import { useCallback, useEffect, useRef, useState } from 'react';

import { fetchHealth } from '@/api/platform';
import type { HealthReport } from '@/api/platform';
import {
  useSnapshotInvalidation,
  useSnapshotStream,
} from '@/shell/useSnapshotStream';

/**
 * What the connection indicator is allowed to claim.
 *
 * `degraded` is distinct from `down`: a 503 with a component report is the
 * platform successfully explaining its own failure. `reconnecting` is also
 * distinct because native EventSource is actively recovering the stream.
 */
export type Liveness = 'connecting' | 'live' | 'degraded' | 'reconnecting' | 'down';

export interface LivenessState {
  status: Liveness;
  report: HealthReport | null;
  /** When the SSE transport last opened or delivered a valid event. */
  lastContact: Date | null;
  /** How this is being measured, so the UI never implies a poll it no longer uses. */
  transport: 'sse';
  detail: string | null;
}

interface HealthSnapshot {
  report: HealthReport | null;
  error: string | null;
}

function reason(error: unknown): string {
  return error instanceof Error ? error.message : 'health snapshot failed';
}

/** Live gateway connection plus an authoritative component-health snapshot. */
export function useLiveness(): LivenessState {
  const [health, setHealth] = useState<HealthSnapshot>({ report: null, error: null });
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);

  const refetchHealth = useCallback((): Promise<void> => {
    if (inFlight.current !== null) return inFlight.current;

    const requestController = new AbortController();
    controller.current = requestController;
    const request = fetchHealth(requestController.signal)
      .then((report) => {
        if (!requestController.signal.aborted) setHealth({ report, error: null });
      })
      .catch((error: unknown) => {
        if (!requestController.signal.aborted) {
          setHealth((previous) => ({ ...previous, error: reason(error) }));
        }
      })
      .finally(() => {
        inFlight.current = null;
        controller.current = null;
      });
    inFlight.current = request;
    return request;
  }, []);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  const { stream } = useSnapshotStream();
  useSnapshotInvalidation(HEALTH_RESOURCES, refetchHealth);

  if (stream.status === 'connecting') {
    return {
      status: 'connecting',
      report: health.report,
      lastContact: stream.lastContact,
      transport: 'sse',
      detail: stream.error,
    };
  }
  if (stream.status === 'reconnecting') {
    return {
      status: 'reconnecting',
      report: health.report,
      lastContact: stream.lastContact,
      transport: 'sse',
      detail: stream.error,
    };
  }
  if (stream.status === 'down') {
    return {
      status: 'down',
      report: health.report,
      lastContact: stream.lastContact,
      transport: 'sse',
      detail: stream.error,
    };
  }
  if (health.report === null) {
    return {
      status: health.error === null ? 'connecting' : 'degraded',
      report: null,
      lastContact: stream.lastContact,
      transport: 'sse',
      detail: health.error,
    };
  }
  return {
    status:
      health.error !== null
        ? 'degraded'
        : health.report.status === 'ready'
          ? 'live'
          : 'degraded',
    report: health.report,
    lastContact: stream.lastContact,
    transport: 'sse',
    detail: health.error ?? stream.error,
  };
}

const HEALTH_RESOURCES = ['health'] as const;
