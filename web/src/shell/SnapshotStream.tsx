import {
  type ReactNode,
  useCallback,
  useMemo,
  useRef,
} from 'react';

import type { SnapshotInvalidation, SnapshotResource } from '@/contracts/types';
import { useEventStream } from '@/shell/useEventStream';
import {
  SnapshotStreamContext,
  type SnapshotRefetch,
} from '@/shell/useSnapshotStream';

/**
 * The command centre's single EventSource and its snapshot-owner registry.
 *
 * Components own REST state. This provider owns only transport and routes a
 * typed invalidation to those owners; it never caches their data.
 */
export function SnapshotStreamProvider({ children }: { children: ReactNode }) {
  const subscribers = useRef(
    new Map<SnapshotResource, Set<SnapshotRefetch>>(),
  );

  const invoke = useCallback((resources: readonly SnapshotResource[] | null) => {
    const selected = new Set<SnapshotRefetch>();
    if (resources === null || resources.includes('all')) {
      for (const callbacks of subscribers.current.values()) {
        for (const callback of callbacks) selected.add(callback);
      }
    } else {
      for (const resource of resources) {
        for (const callback of subscribers.current.get(resource) ?? []) selected.add(callback);
      }
    }
    for (const callback of selected) {
      void Promise.resolve()
        .then(callback)
        .catch(() => undefined);
    }
  }, []);

  const refetchAll = useCallback(() => invoke(null), [invoke]);
  const receive = useCallback(
    (event: SnapshotInvalidation) => invoke(event.resources),
    [invoke],
  );
  const stream = useEventStream({
    refetchSnapshots: refetchAll,
    onEvent: receive,
  });

  const subscribe = useCallback(
    (resources: readonly SnapshotResource[], refetch: SnapshotRefetch) => {
      for (const resource of resources) {
        const callbacks = subscribers.current.get(resource) ?? new Set<SnapshotRefetch>();
        callbacks.add(refetch);
        subscribers.current.set(resource, callbacks);
      }
      return () => {
        for (const resource of resources) {
          const callbacks = subscribers.current.get(resource);
          callbacks?.delete(refetch);
          if (callbacks?.size === 0) subscribers.current.delete(resource);
        }
      };
    },
    [],
  );
  const value = useMemo(() => ({ stream, subscribe }), [stream, subscribe]);

  return (
    <SnapshotStreamContext.Provider value={value}>
      {children}
    </SnapshotStreamContext.Provider>
  );
}
