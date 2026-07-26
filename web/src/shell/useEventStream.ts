import { useEffect, useRef, useState } from 'react';

import {
  parseSnapshotInvalidation,
  STREAM_EVENT_NAME,
  STREAM_URL,
} from '@/api/stream';
import type { SnapshotInvalidation } from '@/contracts/types';

export type EventStreamStatus = 'connecting' | 'open' | 'reconnecting' | 'down';

export interface EventStreamState {
  status: EventStreamStatus;
  lastContact: Date | null;
  error: string | null;
}

interface EventStreamOptions {
  refetchSnapshots: () => void | Promise<void>;
  onEvent?: (event: SnapshotInvalidation) => void;
  url?: string;
}

function reason(error: unknown): string {
  return error instanceof Error ? error.message : 'unknown stream error';
}

/**
 * One native EventSource connection for the command shell.
 *
 * EventSource owns retry timing. Every successful open, including a re-open,
 * refetches authoritative snapshots; stream messages only invalidate them.
 * That keeps a missed message or gateway restart from leaving stale UI state.
 */
export function useEventStream({
  refetchSnapshots,
  onEvent,
  url = STREAM_URL,
}: EventStreamOptions): EventStreamState {
  const [state, setState] = useState<EventStreamState>({
    status: 'connecting',
    lastContact: null,
    error: null,
  });
  const refetchRef = useRef(refetchSnapshots);
  const onEventRef = useRef(onEvent);

  useEffect(() => {
    refetchRef.current = refetchSnapshots;
    onEventRef.current = onEvent;
  }, [onEvent, refetchSnapshots]);

  useEffect(() => {
    let source: EventSource;
    try {
      source = new EventSource(url);
    } catch (error) {
      setState({ status: 'down', lastContact: null, error: reason(error) });
      return;
    }

    const opened = () => {
      setState({ status: 'open', lastContact: new Date(), error: null });
      // The snapshot owner handles its own error state. A failed snapshot read
      // does not mean the SSE connection itself failed.
      void Promise.resolve()
        .then(() => refetchRef.current())
        .catch(() => undefined);
    };
    const failed = () => {
      setState((previous) => ({
        ...previous,
        status: source.readyState === 2 ? 'down' : 'reconnecting',
        error:
          source.readyState === 2
            ? 'event stream closed'
            : 'event stream disconnected; reconnecting automatically',
      }));
    };
    const received: EventListener = (rawEvent) => {
      try {
        if (!(rawEvent instanceof MessageEvent) || typeof rawEvent.data !== 'string') {
          throw new Error('invalid stream event: data is not text');
        }
        const event = parseSnapshotInvalidation(rawEvent.data);
        setState({ status: 'open', lastContact: new Date(), error: null });
        onEventRef.current?.(event);
      } catch (error) {
        setState((previous) => ({ ...previous, error: reason(error) }));
      }
    };

    source.onopen = opened;
    source.onerror = failed;
    source.addEventListener(STREAM_EVENT_NAME, received);

    return () => {
      source.removeEventListener(STREAM_EVENT_NAME, received);
      source.close();
    };
  }, [url]);

  return state;
}
