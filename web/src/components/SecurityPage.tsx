import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';

import { fetchSecurity, SecurityCredentialRequiredError } from '@/api/security';
import { DecompositionChart } from '@/components/DecompositionChart';
import type {
  SecurityCohort,
  SecurityMeasurement,
  SecurityMitigation,
  SecurityResponse,
  SecuritySnapshot,
} from '@/contracts/types';
import { OperatorCredentialPrompt } from '@/shell/OperatorCredential';
import { useAudience } from '@/shell/useAudience';
import { useOperatorCredential } from '@/shell/useOperatorCredential';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

type Load =
  | { state: 'loading' }
  | { state: 'locked'; detail?: string }
  | { state: 'ready'; response: SecurityResponse }
  | { state: 'error'; detail: string };

const SECURITY_RESOURCES = ['security', 'actions', 'incidents'] as const;

const FEATURE_LABELS: Record<SecurityMeasurement['feature'], string> = {
  PATH_ENTROPY: 'Path diversity',
  SOURCE_ENTROPY: 'Source diversity',
  AUTH_FAILURE_RATIO: 'Authentication failures',
  ASN_REPUTATION: 'Network reputation',
  SESSION_ENTROPY: 'Session diversity',
  MACHINE_TIMING: 'Machine-like timing',
  PROTECTED_COHORT_INTEGRITY: 'Protected cohort integrity',
};

function words(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

function title(value: string): string {
  const rendered = words(value);
  return rendered.charAt(0).toUpperCase() + rendered.slice(1);
}

function timestamp(value: string): string {
  return new Intl.DateTimeFormat('en', {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'UTC',
  }).format(new Date(value));
}

function number(value: number): string {
  return Number.isInteger(value) ? value.toLocaleString() : value.toPrecision(4);
}

function measurementValue(measurement: SecurityMeasurement): string {
  if (measurement.status === 'INSUFFICIENT' || measurement.value === null) return 'Insufficient';
  if (
    measurement.unit === 'RATIO' ||
    measurement.unit === 'REPUTATION_SCORE' ||
    measurement.unit === 'INTEGRITY_RATIO' ||
    measurement.unit === 'DEFORMATION_SCORE'
  ) {
    return `${Math.round(measurement.value * 100)}%`;
  }
  return number(measurement.value);
}

function Section({
  id,
  heading,
  description,
  children,
}: {
  id: string;
  heading: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4" aria-labelledby={id}>
      <div>
        <h2 id={id} className="font-serif text-base">
          {heading}
        </h2>
        <p className="text-muted mt-1 max-w-3xl text-xs">{description}</p>
      </div>
      {children}
    </section>
  );
}

function TechnicalEvidence({ measurement }: { measurement: SecurityMeasurement }) {
  const { audience } = useAudience();
  if (audience !== 'technical') return null;
  if (measurement.status === 'INSUFFICIENT') {
    return <p className="text-muted mt-2 text-[11px]">No evidence window or numeric value exists.</p>;
  }
  return (
    <dl className="text-muted mt-3 grid gap-1 text-[11px]">
      <div>
        <dt className="inline">Scope: </dt>
        <dd className="inline font-mono">{measurement.scope}</dd>
      </div>
      <div>
        <dt className="inline">Measured / baseline: </dt>
        <dd className="inline">
          {number(measurement.value!)} / {number(measurement.baseline!)} {words(measurement.unit!)}
        </dd>
      </div>
      <div>
        <dt className="inline">Evidence window: </dt>
        <dd className="inline">
          {timestamp(measurement.window_start!)} → {timestamp(measurement.window_end!)} UTC ·{' '}
          {measurement.window_count} window{measurement.window_count === 1 ? '' : 's'}
        </dd>
      </div>
      <div>
        <dt className="inline">Evidence refs: </dt>
        <dd className="inline font-mono">{measurement.evidence_refs.join(' · ')}</dd>
      </div>
    </dl>
  );
}

function SignalCards({ measurements }: { measurements: SecuritySnapshot['measurements'] }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {measurements.map((measurement) => (
        <article className="border-line bg-sidebar rounded-lg border p-4" key={measurement.feature}>
          <div className="flex items-start justify-between gap-3">
            <h3 className="text-sm font-medium">{FEATURE_LABELS[measurement.feature]}</h3>
            <strong
              className={
                measurement.status === 'MEASURED'
                  ? 'text-accent text-sm'
                  : 'text-muted text-xs'
              }
            >
              {measurementValue(measurement)}
            </strong>
          </div>
          <p className="text-muted mt-2 text-xs">{measurement.detail}</p>
          <TechnicalEvidence measurement={measurement} />
        </article>
      ))}
    </div>
  );
}

function Timeline({ snapshot }: { snapshot: SecuritySnapshot }) {
  const { audience } = useAudience();
  if (snapshot.timeline.length === 0) {
    return (
      <div className="border-line bg-sidebar rounded-lg border p-4 text-sm">
        <strong>No referenced security episode.</strong>
        <p className="text-muted mt-1 text-xs">
          This absence is not proof of quiet; no qualifying residual or ratio episode was attached
          to the exact security assessment.
        </p>
      </div>
    );
  }
  return (
    <ol className="border-line bg-sidebar divide-line divide-y rounded-lg border">
      {snapshot.timeline.map((event) => (
        <li className="grid gap-2 p-4 sm:grid-cols-[10rem_1fr]" key={event.episode_id}>
          <div className="text-xs">
            <p className="font-medium">{timestamp(event.opened_at)} UTC</p>
            <p className="text-muted mt-1">{title(event.status)}</p>
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <strong className="text-sm">{title(event.kind)}</strong>
              <span className="border-line rounded border px-2 py-0.5 font-mono text-[11px]">
                {event.service} · {event.signal}
              </span>
            </div>
            <p className="text-muted mt-2 text-xs">{event.detail}</p>
            <p className="mt-2 text-xs">
              Peak deformation {Math.round(event.peak_deformation_score * 100)}% across{' '}
              {event.breach_window_count} breach window
              {event.breach_window_count === 1 ? '' : 's'}.
            </p>
            {audience === 'technical' && (
              <p className="text-muted mt-1 font-mono text-[11px]">
                {event.episode_id} · refs {event.evidence_refs.join(' · ')}
              </p>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

function CohortCell({ measurement }: { measurement: SecurityMeasurement }) {
  return (
    <td className="px-3 py-3 align-top">
      <strong
        className={measurement.status === 'INSUFFICIENT' ? 'text-muted font-normal' : 'font-medium'}
      >
        {measurementValue(measurement)}
      </strong>
      {measurement.status === 'MEASURED' && (
        <span className="text-muted mt-1 block text-[10px]">{words(measurement.unit!)}</span>
      )}
    </td>
  );
}

function Cohorts({ cohorts }: { cohorts: SecurityCohort[] }) {
  const { audience } = useAudience();
  if (cohorts.length === 0) {
    return (
      <div className="border-line bg-sidebar rounded-lg border p-4 text-sm">
        <strong>No evidence-owned suspect cohort.</strong>
        <p className="text-muted mt-1 text-xs">
          Sentinel will not infer an attacker group from a service name, private label, or verdict.
        </p>
      </div>
    );
  }
  return (
    <div
      aria-label="Scrollable suspect cohort evidence table"
      className="border-line overflow-x-auto rounded-lg border"
      role="region"
      tabIndex={0}
    >
      <table className="w-full min-w-[58rem] text-left text-xs">
        <thead className="bg-sidebar text-muted">
          <tr>
            <th className="px-3 py-3">Cohort</th>
            <th className="px-3 py-3">Requests</th>
            <th className="px-3 py-3">Source diversity</th>
            <th className="px-3 py-3">Auth failures</th>
            <th className="px-3 py-3">Network reputation</th>
            <th className="px-3 py-3">Session diversity</th>
            <th className="px-3 py-3">Machine timing</th>
          </tr>
        </thead>
        <tbody>
          {cohorts.map((cohort) => (
            <tr className="border-line border-t" key={cohort.cohort_id}>
              <td className="px-3 py-3 align-top">
                <strong className="font-mono">{cohort.cohort_id}</strong>
                <span className="text-muted mt-1 block max-w-52">{cohort.detail}</span>
                {audience === 'technical' && (
                  <span className="text-muted mt-1 block font-mono text-[10px]">
                    refs {cohort.evidence_refs.join(' · ')}
                  </span>
                )}
              </td>
              <td className="px-3 py-3 align-top">{cohort.request_count.toLocaleString()}</td>
              {cohort.measurements.map((measurement) => (
                <CohortCell measurement={measurement} key={measurement.feature} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Mitigation({ mitigation }: { mitigation: SecurityMitigation }) {
  const integrity = mitigation.protected_cohort_integrity;
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <article className="border-line bg-sidebar rounded-lg border p-4">
        <h3 className="text-sm font-medium">Current mitigation</h3>
        {mitigation.state === null ? (
          <p className="text-muted mt-2 text-xs">{mitigation.detail}</p>
        ) : (
          <>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <strong className="bg-accent-soft text-ink rounded px-2 py-1 text-xs">
                {title(mitigation.state)}
              </strong>
              <span className="text-xs">
                {title(mitigation.action_kind!)} · {mitigation.target_ref}
              </span>
            </div>
            <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-muted">Plan revision</dt>
                <dd>{mitigation.plan_revision}</dd>
              </div>
              <div>
                <dt className="text-muted">Ladder rung</dt>
                <dd className="font-mono">{mitigation.rung_id}</dd>
              </div>
              <div>
                <dt className="text-muted">Estimated blast radius</dt>
                <dd>{Math.round(mitigation.estimated_blast_fraction! * 100)}%</dd>
              </div>
              <div>
                <dt className="text-muted">Updated</dt>
                <dd>{timestamp(mitigation.updated_at!)} UTC</dd>
              </div>
            </dl>
            <p className="text-muted mt-3 text-xs">{mitigation.detail}</p>
          </>
        )}
      </article>
      <article className="border-line bg-sidebar rounded-lg border p-4">
        <h3 className="text-sm font-medium">Protected cohort integrity</h3>
        <p
          className={
            integrity.status === 'MEASURED'
              ? 'text-ok mt-3 text-2xl font-semibold'
              : 'text-muted mt-3 text-lg font-semibold'
          }
        >
          {measurementValue(integrity)}
        </p>
        <p className="text-muted mt-2 text-xs">{integrity.detail}</p>
        <TechnicalEvidence measurement={integrity} />
      </article>
    </div>
  );
}

function SecurityProof({ snapshot }: { snapshot: SecuritySnapshot }) {
  const decomposition = snapshot.decomposition;
  const peak =
    decomposition.status === 'available'
      ? decomposition.frames.reduce((strongest, frame) =>
          frame.residual_score > strongest.residual_score ? frame : strongest,
        )
      : null;
  return (
    <article className="flex flex-col gap-10">
      <header className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="bg-accent-soft text-ink rounded px-2 py-1 font-medium">
            {snapshot.honesty}
          </span>
          <span className="border-line rounded border px-2 py-1">{title(snapshot.state)}</span>
          <span className="text-muted">Evidence-only projection</span>
        </div>
        <div>
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Security evidence</h1>
          <p className="text-muted mt-2 max-w-3xl text-sm">
            The active incident’s hostile-behavior evidence, suspect cohorts, and current
            mitigation. Event volume is removed before the residual is judged.
          </p>
        </div>
        <dl className="grid gap-2 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-muted">Incident</dt>
            <dd className="font-mono">{snapshot.incident_id}</dd>
          </div>
          <div>
            <dt className="text-muted">Window</dt>
            <dd>
              {timestamp(snapshot.opened_at)} → {timestamp(snapshot.updated_at)} UTC
            </dd>
          </div>
          <div>
            <dt className="text-muted">Evidence-owned cohorts</dt>
            <dd>{snapshot.suspect_cohorts.length}</dd>
          </div>
        </dl>
      </header>

      <Section
        id="security-residual"
        heading="Residual decomposition"
        description="Observed volume is split into ordinary baseline, named-event contribution, and unexplained behavior."
      >
        {decomposition.status === 'insufficient' || peak === null ? (
          <div className="border-line bg-sidebar rounded-lg border p-4 text-sm">
            <strong>Insufficient incident-window decomposition.</strong>
            <p className="text-muted mt-1 text-xs">{decomposition.detail}</p>
          </div>
        ) : (
          <div className="border-line bg-raised rounded-lg border p-4">
            <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
              <strong>{decomposition.service}</strong>
              <code>{decomposition.signal}</code>
              <span className="text-muted">
                Peak residual score {Math.round(peak.residual_score * 100)}%
              </span>
            </div>
            <DecompositionChart frames={decomposition.frames} />
            <p className="text-muted mt-3 font-mono text-[11px]">
              {number(peak.observed)} observed = {number(peak.explained_base)} explained base +{' '}
              {number(peak.explained_event)} explained event + {number(peak.residual)} unexplained
              residual
            </p>
          </div>
        )}
      </Section>

      <Section
        id="security-timeline"
        heading="Attack timeline"
        description="Only residual or ratio episodes referenced by the exact security assessment appear here."
      >
        <Timeline snapshot={snapshot} />
      </Section>

      <Section
        id="security-signals"
        heading="Security signals"
        description="Every expected signal is present. Missing telemetry remains insufficient, never numeric zero."
      >
        <SignalCards measurements={snapshot.measurements} />
      </Section>

      <Section
        id="security-cohorts"
        heading="Suspect cohort evidence"
        description="Cohort names come from explicit public telemetry evidence; Sentinel does not manufacture identity from a verdict."
      >
        <Cohorts cohorts={snapshot.suspect_cohorts} />
      </Section>

      <Section
        id="security-mitigation"
        heading="Mitigation and protected traffic"
        description="The latest server-held action is shown separately from independent evidence about protected traffic."
      >
        <Mitigation mitigation={snapshot.mitigation} />
      </Section>

      <footer className="border-line text-muted border-t pt-4 text-xs">
        <strong className="text-ink">{snapshot.honesty}</strong> telemetry provenance. This view
        renders deterministic stored evidence; it does not execute or propose an action.
      </footer>
    </article>
  );
}

export function SecurityPage() {
  const { credential, clearCredential } = useOperatorCredential();
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const refetchQueued = useRef(false);

  const refetch = useCallback((): Promise<void> => {
    if (inFlight.current !== null) {
      refetchQueued.current = true;
      return inFlight.current;
    }
    const requestController = new AbortController();
    controller.current = requestController;
    const request = fetchSecurity(credential, requestController.signal)
      .then((response) => {
        if (!requestController.signal.aborted) setLoad({ state: 'ready', response });
      })
      .catch((error: unknown) => {
        if (!requestController.signal.aborted && error instanceof SecurityCredentialRequiredError) {
          clearCredential();
          setLoad({ state: 'locked' });
        } else if (!requestController.signal.aborted) {
          setLoad({
            state: 'error',
            detail: error instanceof Error ? error.message : String(error),
          });
        }
      })
      .finally(() => {
        inFlight.current = null;
        controller.current = null;
        if (refetchQueued.current && !requestController.signal.aborted) {
          refetchQueued.current = false;
          void refetch();
        }
      });
    inFlight.current = request;
    return request;
  }, [clearCredential, credential]);

  useSnapshotInvalidation(SECURITY_RESOURCES, refetch);
  useEffect(() => {
    setLoad({ state: 'loading' });
    void refetch();
    return () => controller.current?.abort();
  }, [refetch]);

  if (load.state === 'loading') {
    return (
      <p className="text-muted text-sm" role="status">
        Reading the current security evidence…
      </p>
    );
  }
  if (load.state === 'locked') {
    return (
      <OperatorCredentialPrompt
        buttonLabel="Unlock security evidence"
        detail={load.detail}
        title="Protected security evidence"
      />
    );
  }
  if (load.state === 'error') {
    return (
      <div className="flex flex-col gap-3" role="alert">
        <h1 className="text-2xl font-semibold">Security evidence unavailable</h1>
        <p className="text-bad text-sm">{load.detail}</p>
        <Link className="text-accent text-sm underline" to="/command">
          Return to command center
        </Link>
      </div>
    );
  }
  if (load.response.snapshot === null) {
    const degraded = load.response.status === 'degraded';
    return (
      <div className="flex max-w-2xl flex-col gap-3" role={degraded ? 'alert' : undefined}>
        <h1 className="text-2xl font-semibold">
          {degraded ? 'Security evidence unavailable' : 'No current security snapshot'}
        </h1>
        <p className={degraded ? 'text-bad text-sm' : 'text-muted text-sm'}>
          {load.response.detail}
        </p>
        <p className="text-muted text-xs">
          An absent security snapshot is not evidence that the system is safe or quiet.
        </p>
        <Link className="text-accent text-sm underline" to="/command">
          Return to command center
        </Link>
      </div>
    );
  }
  return <SecurityProof snapshot={load.response.snapshot} />;
}
