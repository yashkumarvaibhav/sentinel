import { useCallback, useEffect, useRef, useState } from 'react';

import { LabCredentialRequiredError, fetchLabFeed, fireScenario } from '@/api/lab';
import type { LabRunFeed, LabRunMode, LabRunSnapshot, LabScenarioOption } from '@/contracts/types';
import { OperatorCredentialPrompt } from '@/shell/OperatorCredential';
import { useOperatorCredential } from '@/shell/useOperatorCredential';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

const LAB_RESOURCES = ['lab'] as const;

type Load =
  | { state: 'loading' }
  | { state: 'locked'; detail?: string }
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
      : run.state === 'RUNNING' || run.state === 'QUEUED'
        ? 'bg-accent-soft text-accent'
        : 'bg-bad-soft text-bad';
  return (
    <span className={`rounded px-2 py-0.5 text-[0.7rem] font-semibold ${tone}`}>{run.state}</span>
  );
}

function Run({ run }: { run: LabRunSnapshot }) {
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
      {run.incident_id !== null && (
        <a
          className="text-accent mt-1 inline-block text-xs underline"
          href={`/incidents/${run.incident_id}`}
        >
          Open the incident it produced
        </a>
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
  pending,
  onFire,
}: {
  scenario: LabScenarioOption;
  disabled: boolean;
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
            disabled={disabled}
            key={mode}
            onClick={() => onFire(scenario.scenario_id, mode)}
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
        setLoad({ state: 'locked' });
        return;
      }
      setLoad({ state: 'error', detail: (error as Error).message });
    }
  }, [credential]);

  useEffect(() => {
    void refresh();
    return () => inFlight.current?.abort();
  }, [refresh]);

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
          setLoad({ state: 'locked' });
        } else {
          setLoad({ state: 'error', detail: (error as Error).message });
        }
      } finally {
        setPending(null);
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
        <h2 className="text-ink text-lg font-semibold" id="demo-locked">
          Demo launcher
        </h2>
        <p className="text-muted mt-1 text-sm">
          Firing a scenario changes what the platform is looking at, so it needs the operator
          credential.
        </p>
        <OperatorCredentialPrompt
          buttonLabel="Unlock the launcher"
          detail={undefined}
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
        <h2 className="text-ink text-lg font-semibold" id="demo-heading">
          Demo launcher
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
              <Run key={run.run_id} run={run} />
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
