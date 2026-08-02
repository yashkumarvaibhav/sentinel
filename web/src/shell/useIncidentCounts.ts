import { useCallback, useEffect, useState } from 'react';

import { fetchIncidents } from '@/api/incidents';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

const INCIDENT_RESOURCES = ['incidents'] as const;

export interface IncidentCounts {
  /** Recorded in the current window. `null` until something has actually been read. */
  total: number | null;
  /** Recorded and not yet resolved. */
  unresolved: number | null;
}

/**
 * The number beside the nav item.
 *
 * `null` rather than `0` until a read succeeds, and the badge renders nothing in
 * that state. A zero here would be the same lie the empty feed refuses to tell:
 * "nothing recorded" and "we have not managed to look" are different facts, and
 * a confident `0` on a console whose gateway is unreachable is the worse of the
 * two to show.
 */
export function useIncidentCounts(): IncidentCounts {
  const [counts, setCounts] = useState<IncidentCounts>({ total: null, unresolved: null });

  const read = useCallback((): Promise<void> => {
    const controller = new AbortController();
    return fetchIncidents(controller.signal, 200)
      .then((response) => {
        if (controller.signal.aborted) return;
        if (response.status !== 'ready') {
          setCounts({ total: null, unresolved: null });
          return;
        }
        setCounts({
          total: response.incidents.length,
          unresolved: response.incidents.filter((item) => item.state !== 'RESOLVED').length,
        });
      })
      .catch(() => {
        // Unreachable gateway: fall back to "we do not know", never to zero.
        setCounts({ total: null, unresolved: null });
      });
  }, []);

  useSnapshotInvalidation(INCIDENT_RESOURCES, read);
  useEffect(() => {
    void read();
  }, [read]);

  return counts;
}
