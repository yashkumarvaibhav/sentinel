import { useCallback, useEffect, useRef, useState } from 'react';

import {
  LabCredentialRequiredError,
  controlScenario,
  fetchLabFeed,
  fireScenario,
} from '@/api/lab';
import type {
  LabRunControl,
  LabRunFeed,
  LabRunMode,
  LabRunSnapshot,
  LabScenarioOption,
} from '@/contracts/types';
import { OperatorCredentialPrompt } from '@/shell/OperatorCredential';
import { useOperatorCredential } from '@/shell/useOperatorCredential';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { Button } from '@/ui/Button';
import { Pause, Play, Square } from '@/ui/icons';

const LAB_RESOURCES = ['lab'] as const;

type Load =
  | { state: 'loading' }
  | { state: 'locked'; detail?: string | undefined }
  | { state: 'ready'; feed: LabRunFeed }
  | { state: 'error'; detail: string };

function minutes(seconds: number): string {
  if (seconds < 90) return `${seconds}s`;
  return `about ${Math.round(seconds / 60)} min`;
}

function StateChip({ run }: { run: LabRunSnapshot }) {
  const tone =
    run.state === 'SUCCEEDED'
      ? 'bg-good-soft text-good'
      : run.state === 'RUNNING' || run.state === 'QUEUED' || run.state === 'PAUSED'
        ? 'bg-accent-soft text-ink'
        : 'bg-bad-soft text-bad';
  return (
    <span className={`rounded px-2 py-0.5 text-[0.7rem] font-semibold ${tone}`}>{run.state}</span>
  );
}

/** How long a queued run may wait before the silence is worth explaining. */
const UNCLAIMED_GRACE_MS = 45_000;

/**
 * Why a queued run appears to do nothing.
 *
 * A current runner heartbeat gates new work. A claimed runner can still become
 * unhealthy immediately afterwards, though, so a queued run that does not move
 * deserves an explicit operator-facing explanation rather than silent waiting.
 *
 * So the wait itself is named as soon as it is longer than a claim should take,
 * along with the one command that fixes it.
 */
function UnclaimedNotice({ run }: { run: LabRunSnapshot }) {
  const [now, setNow] = useState(() => Date.now());
  const queued = run.state === 'QUEUED';

  useEffect(() => {
    if (!queued) return;
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, [queued]);

  if (!queued) return null;
  const waited = now - new Date(run.requested_at).getTime();
  if (waited < UNCLAIMED_GRACE_MS) return null;

  return (
    <p className="text-warn mt-1 text-xs" role="status">
      No runner has claimed this in {Math.max(Math.round(waited / 1000), 1)}s. Check the measured
      runner status below; if it has stopped, restart it beside the stack with{' '}
      <code className="font-mono">make demo-runner</code>.
    </p>
  );
}

function Run({
  run,
  pending,
  onControl,
}: {
  run: LabRunSnapshot;
  pending: LabRunControl | null;
  onControl: (runId: string, control: LabRunControl) => void;
}) {
  const controllable = ['QUEUED', 'RUNNING', 'PAUSED'].includes(run.state);
  return (
    <li className="border-line border-t py-3 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        <StateChip run={run} />
        <span className="text-ink text-sm font-medium">{run.scenario_id}</span>
        <span className="text-muted text-xs">{run.mode}</span>
        <span className="text-muted ml-auto text-xs">
          {new Date(run.requested_at).toLocaleTimeString('en', { timeZone: 'UTC' })} UTC
        </span>
      </div>
      <p className="text-muted mt-1 text-xs">{run.detail}</p>
      <UnclaimedNotice run={run} />
      {run.incident_id !== null && (
        <a
          className="text-accent mt-1 inline-block text-xs underline"
          href={`/incidents/${run.incident_id}`}
        >
          Open the incident it produced
        </a>
      )}
      {controllable && (
        <div className="mt-2 flex flex-wrap gap-2" role="group" aria-label={`Control ${run.scenario_id}`}>
          {run.state === 'PAUSED' ? (
            <Button
              disabled={pending !== null}
              icon={Play}
              onClick={() => onControl(run.run_id, 'RESUME')}
              variant="primary"
            >
              {pending === 'RESUME' ? 'Resuming…' : 'Resume from start'}
            </Button>
          ) : (
            <Button
              disabled={pending !== null || run.control_requested !== null}
              icon={Pause}
              onClick={() => onControl(run.run_id, 'PAUSE')}
            >
              {pending === 'PAUSE' || run.control_requested === 'PAUSE' ? 'Pausing…' : 'Pause'}
            </Button>
          )}
          <Button
            disabled={pending !== null || run.control_requested !== null}
            icon={Square}
            onClick={() => onControl(run.run_id, 'STOP')}
            variant="danger"
          >
            {pending === 'STOP' || run.control_requested === 'STOP' ? 'Stopping…' : 'Stop'}
          </Button>
        </div>
      )}
      <dl className="text-muted mt-2 grid gap-x-4 gap-y-0.5 text-[0.7rem] sm:grid-cols-3">
        <div>
          <dt className="inline font-semibold">Telemetry: </dt>
          <dd className="inline">{run.honesty.telemetry}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Stimulus: </dt>
          <dd className="inline">{run.honesty.stimulus}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">Reproducibility: </dt>
          <dd className="inline">{run.honesty.reproducibility}</dd>
        </div>
      </dl>
    </li>
  );
}

function Scenario({
  scenario,
  disabled,
  liveReady,
  pending,
  onFire,
}: {
  scenario: LabScenarioOption;
  disabled: boolean;
  liveReady: boolean;
  pending: LabRunMode | null;
  onFire: (scenario: string, mode: LabRunMode) => void;
}) {
  return (
    <li className="border-line rounded-lg border p-4">
      <h3 className="text-ink text-sm font-semibold">{scenario.name}</h3>
      <p className="text-muted mt-1 text-xs">{scenario.description}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {scenario.modes.map((mode) => (
          <button
            className="border-line text-ink hover:bg-accent-soft disabled:text-muted min-h-11 rounded-md border px-3 text-xs font-medium disabled:cursor-not-allowed sm:min-h-9"
            disabled={disabled || (mode === 'LIVE' && !liveReady)}
            key={mode}
            onClick={() => {
              if (mode !== 'LIVE' || liveReady) onFire(scenario.scenario_id, mode);
            }}
            type="button"
          >
            {pending === mode ? 'Queueing…' : mode === 'REPLAY' ? 'Replay a capture' : 'Run live'}
            {mode === 'LIVE' && typeof scenario.live_duration_seconds === 'number' && (
              <span className="text-muted"> · {minutes(scenario.live_duration_seconds)}</span>
            )}
          </button>
        ))}
      </div>
    </li>
  );
}

/**
 * Fire a scenario at the testbed and watch the platform catch it.
 *
 * The button records intent and nothing else. The gateway image carries no
 * `lab/`, so the process serving this page cannot execute a scenario even if
 * it wanted to — a separate runner with the repo mounted claims the queued row
 * and does the work. That is why the page can be honest about being
 * unavailable: "no runner attached" is a real, distinct state, not a failure.
 */
export function DemoLauncher() {
  const { credential } = useOperatorCredential();
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [pending, setPending] = useState<{ scenario: string; mode: LabRunMode } | null>(null);
  const [pendingControl, setPendingControl] = useState<{
    runId: string;
    control: LabRunControl;
  } | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    try {
      setLoad({ state: 'ready', feed: await fetchLabFeed(credential, controller.signal) });
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof LabCredentialRequiredError) {
        // A credential that was offered and refused is a different state from
        // one never given. Rendering both as a blank prompt is why a wrong key
        // looked like nothing happening at all.
        setLoad({
          state: 'locked',
          detail:
            credential === null
              ? undefined
              : 'That credential was refused. Check it and try again — it is never stored, so a reload clears it.',
        });
        return;
      }
      setLoad({ state: 'error', detail: (error as Error).message });
    }
  }, [credential]);

  useEffect(() => {
    void refresh();
    return () => inFlight.current?.abort();
  }, [refresh]);

  useEffect(() => {
    if (load.state !== 'ready') return;
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(timer);
  }, [load.state, refresh]);

  // The runner commits its progress and then notifies, so a page told to
  // refetch is always told about state that is already durable.
  useSnapshotInvalidation(LAB_RESOURCES, refresh);

  const fire = useCallback(
    async (scenario: string, mode: LabRunMode) => {
      setPending({ scenario, mode });
      try {
        const feed = await fireScenario(credential, { scenario_id: scenario, mode });
        setLoad({ state: 'ready', feed });
      } catch (error) {
        if (error instanceof LabCredentialRequiredError) {
          setLoad({
            state: 'locked',
            detail:
              credential === null
                ? undefined
                : 'That credential was refused, so nothing was fired.',
          });
        } else {
          setLoad({ state: 'error', detail: (error as Error).message });
        }
      } finally {
        setPending(null);
      }
    },
    [credential],
  );

  const control = useCallback(
    async (runId: string, requested: LabRunControl) => {
      setPendingControl({ runId, control: requested });
      try {
        setLoad({ state: 'ready', feed: await controlScenario(credential, runId, requested) });
      } catch (error) {
        if (error instanceof LabCredentialRequiredError) {
          setLoad({ state: 'locked', detail: 'That credential was refused, so the run was not changed.' });
        } else {
          setLoad({ state: 'error', detail: (error as Error).message });
        }
      } finally {
        setPendingControl(null);
      }
    },
    [credential],
  );

  if (load.state === 'loading') {
    return <p className="text-muted text-sm">Reading the launcher…</p>;
  }
  if (load.state === 'locked') {
    return (
      <section aria-labelledby="demo-locked">
        {/* Same reason as the ready branch: the route owns the name. */}
        <h2 className="sr-only" id="demo-locked">
          Launcher access
        </h2>
        <p className="text-muted mt-1 text-sm">
          Firing a scenario changes what the platform is looking at, so it needs the operator
          credential.
        </p>
        <OperatorCredentialPrompt
          buttonLabel="Unlock the launcher"
          detail={load.detail}
          title="Protected operator surface"
        />
      </section>
    );
  }
  if (load.state === 'error') {
    return (
      <p className="text-bad text-sm" role="alert">
        {load.detail}
      </p>
    );
  }

  const { feed } = load;
  const busy = feed.status !== 'READY';
  return (
    <section aria-labelledby="demo-heading" className="space-y-6">
      <header>
        {/* The route supplies the `h1`; repeating it here printed the name
            twice under itself. What this header is actually for is the note. */}
        <h2 className="sr-only" id="demo-heading">
          Scenarios
        </h2>
        <p
          aria-live="polite"
          className={`mt-1 text-sm ${feed.status === 'UNAVAILABLE' ? 'text-warn' : 'text-muted'}`}
        >
          {feed.note}
        </p>
      </header>

      <ul className="grid gap-3 sm:grid-cols-2">
        {feed.scenarios.map((scenario) => (
          <Scenario
            disabled={busy || pending !== null}
            liveReady={feed.live_ready}
            key={scenario.scenario_id}
            onFire={(id, mode) => void fire(id, mode)}
            pending={pending?.scenario === scenario.scenario_id ? pending.mode : null}
            scenario={scenario}
          />
        ))}
      </ul>

      <section aria-labelledby="demo-runs">
        <h3 className="text-ink text-sm font-semibold" id="demo-runs">
          Recent runs
        </h3>
        {feed.runs.length === 0 ? (
          <p className="text-muted mt-2 text-xs">
            Nothing has been fired yet. An empty history is not evidence that the launcher works.
          </p>
        ) : (
          <ul className="mt-2">
            {feed.runs.map((run) => (
              <Run
                key={run.run_id}
                run={run}
                pending={pendingControl?.runId === run.run_id ? pendingControl.control : null}
                onControl={(runId, requested) => void control(runId, requested)}
              />
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
