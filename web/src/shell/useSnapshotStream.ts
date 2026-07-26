import { createContext, useContext, useEffect } from 'react';

import type { SnapshotResource } from '@/contracts/types';
import type { EventStreamState } from '@/shell/useEventStream';

export type SnapshotRefetch = () => void | Promise<void>;

export interface SnapshotStreamValue {
  stream: EventStreamState;
  subscribe: (resources: readonly SnapshotResource[], refetch: SnapshotRefetch) => () => void;
}

export const SnapshotStreamContext = createContext<SnapshotStreamValue | null>(null);

export function useSnapshotStream(): SnapshotStreamValue {
  const value = useContext(SnapshotStreamContext);
  if (value === null) {
    throw new Error('useSnapshotStream must be rendered inside SnapshotStreamProvider');
  }
  return value;
}

export function useSnapshotInvalidation(
  resources: readonly SnapshotResource[],
  refetch: SnapshotRefetch,
): void {
  const { subscribe } = useSnapshotStream();
  useEffect(() => subscribe(resources, refetch), [refetch, resources, subscribe]);
}
