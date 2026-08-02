import { useCallback, useEffect, useRef, useState } from 'react';

import { fetchScenarioActivity, type ScenarioActivity } from '@/api/activity';
import { fetchIncidents } from '@/api/incidents';
import { fetchHealth, type HealthReport } from '@/api/platform';
import type { IncidentFeedResponse } from '@/contracts/types';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { Activity, CircleCheck, TriangleAlert } from '@/ui/icons';

interface ConsoleSnapshot {
  activity: ScenarioActivity;
  health: HealthReport;
  incidents: IncidentFeedResponse;
}

const LIVE_RESOURCES = ['health', 'incidents', 'lab'] as const;

function age(lastJudgedAt: string | null, now: number): string {
  if (lastJudgedAt === null) return 'never';
  const seconds = Math.max(Math.floor((now - Date.parse(lastJudgedAt)) / 1000), 0);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3600)}h`;
}

function elapsed(startedAt: string | null, now: number): string {
  if (startedAt === null) return 'queued';
  const seconds = Math.max(Math.floor((now - Date.parse(startedAt)) / 1000), 0);
  return seconds < 60
    ? `${seconds}s`
    : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function TapeCell({
  label,
  value,
  detail,
  tone = 'normal',
}: {
  label: string;
  value: string;
  detail: string;
  tone?: 'normal' | 'active' | 'warning';
}) {
  const Icon = tone === 'warning' ? TriangleAlert : tone === 'active' ? Activity : CircleCheck;
  const color =
    tone === 'warning' ? 'text-danger' : tone === 'active' ? 'text-accent' : 'text-ink';
  return (
    <div className="border-line flex min-w-40 flex-1 items-start gap-2 border-r px-3 py-2 last:border-r-0">
      <Icon
        aria-hidden="true"
        className={`${color} mt-0.5 size-3.5 shrink-0 ${tone === 'active' ? 'motion-safe:animate-pulse' : ''}`}
        strokeWidth={2.25}
      />
      <div className="min-w-0">
        <p className="text-faint text-[10px] tracking-wider uppercase">{label}</p>
        <p className={`${color} mt-0.5 truncate text-sm font-bold tabular-nums`}>{value}</p>
        <p className="text-muted mt-0.5 truncate text-[10px]">{detail}</p>
      </div>
    </div>
  );
}

/**
 * A compact, continuously measured market tape for the command centre.
 *
 * Motion is earned by changing evidence: scenario time/progress, incident
 * states, judgement freshness and dependency probe latency. There are no
 * random numbers or decorative price ticks, so a calm line still means calm
 * evidence rather than a paused animation.
 */
export function LiveOperationsTape() {
  const [snapshot, setSnapshot] = useState<ConsoleSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);

  const read = useCallback((): Promise<void> => {
    if (inFlight.current !== null) return inFlight.current;
    const requestController = new AbortController();
    controller.current = requestController;
    const request = Promise.all([
      fetchScenarioActivity(requestController.signal),
      fetchHealth(requestController.signal),
      fetchIncidents(requestController.signal, 50),
    ])
      .then(([activity, health, incidents]) => {
        if (requestController.signal.aborted) return;
        setSnapshot({ activity, health, incidents });
        setError(null);
        setNow(Date.now());
      })
      .catch((caught: unknown) => {
        if (!requestController.signal.aborted) {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      })
      .finally(() => {
        inFlight.current = null;
        controller.current = null;
      });
    inFlight.current = request;
    return request;
  }, []);

  useSnapshotInvalidation(LIVE_RESOURCES, read);
  useEffect(() => {
    void read();
    return () => controller.current?.abort();
  }, [read]);

  const running = snapshot?.activity.in_flight === true;
  useEffect(() => {
    const timer = window.setInterval(
      () => {
        setNow(Date.now());
        void read();
      },
      running ? 2_000 : 10_000,
    );
    return () => window.clearInterval(timer);
  }, [read, running]);

  if (snapshot === null) {
    return (
      <section
        aria-label="Live operations tape"
        className="border-line bg-sidebar rounded-lg border px-3 py-2"
      >
        <p className={error === null ? 'text-muted text-xs' : 'text-danger text-xs'}>
          {error === null ? 'Opening the live operations tape…' : `Live tape unavailable: ${error}`}
        </p>
      </section>
    );
  }

  const active = snapshot.incidents.incidents.filter((item) => item.state !== 'RESOLVED');
  const attacks = active.filter(
    (item) => item.verdict_class === 'ATTACK' || item.verdict_class === 'COMBINATION',
  );
  const faults = active.filter(
    (item) =>
      item.verdict_class === 'OPERATIONAL_FAULT' || item.verdict_class === 'CODE_CONFIG_FAULT',
  );
  const ready = snapshot.health.components.filter((component) => component.ready).length;
  const slowest = [...snapshot.health.components].sort(
    (left, right) => right.latency_ms - left.latency_ms,
  )[0];
  const activity = snapshot.activity;
  const progress = activity.progress === null ? null : Math.round(activity.progress * 100);
  const stale = snapshot.incidents.observation.status !== 'WATCHING';

  return (
    <section
      aria-label="Live operations tape"
      aria-live="off"
      className="border-line bg-sidebar overflow-x-auto rounded-lg border"
      tabIndex={0}
    >
      <div className="flex min-w-max" data-testid="live-operations-tape">
        <TapeCell
          label="Scenario"
          value={activity.in_flight ? `${activity.scenario_id ?? 'run'} · ${elapsed(activity.started_at, now)}` : 'Idle'}
          detail={
            activity.in_flight
              ? `${activity.mode?.toLowerCase() ?? 'unknown'} · ${progress === null ? activity.state?.toLowerCase() : `${progress}%`}`
              : activity.note
          }
          tone={activity.in_flight ? 'active' : 'normal'}
        />
        <TapeCell
          label="Active incidents"
          value={active.length.toLocaleString()}
          detail={`${active.filter((item) => item.state === 'OPEN').length} open · ${active.filter((item) => item.state === 'MITIGATING').length} mitigating`}
          tone={active.length > 0 ? 'active' : 'normal'}
        />
        <TapeCell
          label="Threats"
          value={attacks.length.toLocaleString()}
          detail={attacks.length > 0 ? `${attacks[0]?.origin_service ?? 'unknown'} origin` : 'no active hostile verdict'}
          tone={attacks.length > 0 ? 'warning' : 'normal'}
        />
        <TapeCell
          label="Faults"
          value={faults.length.toLocaleString()}
          detail={faults.length > 0 ? `${faults[0]?.origin_service ?? 'unknown'} origin` : 'no active fault verdict'}
          tone={faults.length > 0 ? 'warning' : 'normal'}
        />
        <TapeCell
          label="Last judgement"
          value={`${age(snapshot.incidents.observation.last_judged_at ?? null, now)} ago`}
          detail={snapshot.incidents.observation.status.toLowerCase()}
          tone={stale ? 'warning' : running ? 'active' : 'normal'}
        />
        <TapeCell
          label="Platform planes"
          value={`${ready}/${snapshot.health.components.length} ready`}
          detail={slowest === undefined ? 'no dependency probes' : `slowest ${slowest.name} ${Math.round(slowest.latency_ms)}ms`}
          tone={snapshot.health.status === 'degraded' ? 'warning' : 'normal'}
        />
      </div>
    </section>
  );
}
