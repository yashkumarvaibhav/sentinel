import { useCallback, useEffect, useState } from 'react';

import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { LoaderCircle } from '@/ui/icons';

const LAB_RESOURCES = ['lab'] as const;

interface Activity {
  in_flight: boolean;
  scenario_id: string | null;
  mode: string | null;
  state: string | null;
  started_at: string | null;
  note: string;
}

function elapsed(startedAt: string, now: number): string {
  const seconds = Math.max(Math.floor((now - new Date(startedAt).getTime()) / 1000), 0);
  const minutes = Math.floor(seconds / 60);
  return minutes >= 1 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

/**
 * The console saying that something is being done to it right now.
 *
 * A replay writes nothing until it ends, so for the several minutes it runs
 * there is genuinely nothing new to render — and a screen that cannot see the
 * run has no way to distinguish that from a platform doing nothing at all.
 * That is most of why this reads as a static page while a scenario is in play.
 *
 * So the run itself becomes the thing on screen: named, with its mode, and a
 * clock that counts up. It is not decoration standing in for activity — it is
 * the one piece of live state the console was blind to, and it ticks because
 * the elapsed time genuinely changes.
 *
 * It polls rather than only listening: the stream invalidates on *transitions*,
 * and between them the clock still has to move.
 */
export function ScenarioActivityBar() {
  const [activity, setActivity] = useState<Activity | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const read = useCallback((): Promise<void> => {
    const controller = new AbortController();
    return fetch('/api/activity', { signal: controller.signal })
      .then((response) => (response.ok ? (response.json() as Promise<Activity>) : null))
      .then((next) => {
        if (next !== null) setActivity(next);
      })
      .catch(() => {
        // The connection banner already reports an unreachable gateway.
      });
  }, []);

  useSnapshotInvalidation(LAB_RESOURCES, read);
  useEffect(() => {
    void read();
  }, [read]);

  // While something is running the clock has to move on its own, and the run's
  // own state has to be re-read - a replay ends without any other signal.
  useEffect(() => {
    const running = activity?.in_flight === true;
    const timer = setInterval(
      () => {
        setNow(Date.now());
        if (running) void read();
      },
      running ? 3_000 : 20_000,
    );
    return () => clearInterval(timer);
  }, [activity?.in_flight, read]);

  if (activity === null || !activity.in_flight) return null;

  return (
    <div
      role="status"
      className="border-line bg-sidebar flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2.5 sm:px-6 lg:px-8"
    >
      <LoaderCircle
        aria-hidden="true"
        className="text-accent size-4 shrink-0 motion-safe:animate-spin"
        strokeWidth={2.25}
      />
      <span className="text-ink text-sm font-bold">
        {activity.scenario_id} · {activity.mode?.toLowerCase()}
      </span>
      {activity.started_at !== null && (
        <span className="text-accent text-sm font-bold tabular-nums">
          {elapsed(activity.started_at, now)}
        </span>
      )}
      <span className="text-muted text-xs">
        {activity.state === 'QUEUED'
          ? 'Waiting for a runner to claim it.'
          : 'Running. A replay publishes its incident when it finishes, so the feed below stays as it was until then.'}
      </span>
    </div>
  );
}
