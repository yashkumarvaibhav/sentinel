import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import { fetchIncidentDetail } from '@/api/incidentDetail';
import { CredentialRequiredError } from '@/api/actionControl';
import { ActionControlPanel } from '@/components/ActionControlPanel';
import { CausalGraphView } from '@/components/CausalGraph';
import type {
  DecompFrame,
  IncidentDetail,
  IncidentDetailResponse,
  VerdictClass,
} from '@/contracts/types';
import { useAudience } from '@/shell/useAudience';
import { OperatorCredentialPrompt } from '@/shell/OperatorCredential';
import { useOperatorCredential } from '@/shell/useOperatorCredential';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

type Load =
  | { state: 'loading' }
  | { state: 'locked'; detail?: string }
  | { state: 'ready'; response: IncidentDetailResponse }
  | { state: 'error'; detail: string };

const INCIDENT_RESOURCES = ['incidents'] as const;

function words(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

function title(value: string): string {
  const rendered = words(value);
  return rendered.charAt(0).toUpperCase() + rendered.slice(1);
}

function number(value: number): string {
  return Number.isInteger(value) ? value.toLocaleString() : value.toPrecision(4);
}

function timestamp(value: string): string {
  return new Intl.DateTimeFormat('en', {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'UTC',
  }).format(new Date(value));
}

function Section({
  id,
  title: heading,
  description,
  children,
}: {
  id: string;
  title: string;
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

function VerdictDistribution({ detail }: { detail: IncidentDetail }) {
  if (detail.verdict.status === 'insufficient') {
    return (
      <p className="border-line bg-sidebar text-muted rounded-lg border p-4 text-sm">
        The deterministic fusion refused to name a verdict. No distribution is presented as
        evidence.
      </p>
    );
  }
  return (
    <div
      aria-label="Full verdict support distribution"
      className="grid gap-2 sm:grid-cols-5"
      role="group"
    >
      {detail.verdict.distribution.map((point) => (
        <div className="border-line bg-sidebar rounded-lg border p-3" key={point.verdict_class}>
          <div className="flex items-center justify-between gap-2 text-[11px]">
            <span>{title(point.verdict_class)}</span>
            <strong>{Math.round(point.probability * 100)}%</strong>
          </div>
          <div className="bg-line mt-2 h-1.5 overflow-hidden rounded-full">
            <span
              className={
                point.verdict_class === detail.verdict.verdict_class
                  ? 'bg-accent block h-full'
                  : 'bg-muted block h-full'
              }
              style={{ width: `${Math.max(2, point.probability * 100)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

function DecompositionProof({ detail }: { detail: IncidentDetail }) {
  const decomposition = detail.decomposition;
  if (decomposition.status === 'insufficient' || decomposition.frames.length === 0) {
    return (
      <div className="border-line bg-sidebar rounded-lg border p-4 text-sm">
        <strong>Insufficient incident-window decomposition.</strong>
        <p className="text-muted mt-1">{decomposition.detail}</p>
      </div>
    );
  }
  const peak = decomposition.frames.reduce((strongest, frame) =>
    frame.residual_score > strongest.residual_score ? frame : strongest,
  );
  const parts: Array<{ name: string; value: number; color: string }> = [
    { name: 'Explained base', value: peak.explained_base, color: 'var(--color-decomp-base)' },
    { name: 'Explained event', value: peak.explained_event, color: 'var(--color-event)' },
    { name: 'Unexplained residual', value: peak.residual, color: 'var(--color-residual)' },
  ];
  return (
    <div className="grid gap-3">
      <div className="border-line bg-raised rounded-lg border p-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <strong>{decomposition.service}</strong>
          <code>{decomposition.signal}</code>
          <span className="text-muted">Peak at {timestamp(peak.ts)} UTC</span>
          {decomposition.truncated && (
            <span className="text-warn">Frame limit reached; this window is truncated.</span>
          )}
        </div>
        <div className="mt-4 grid gap-3 sm:grid-cols-4">
          <div className="border-line rounded border p-3">
            <p className="text-muted text-[10px] tracking-wide uppercase">Observed</p>
            <p className="mt-1 text-xl font-semibold">{number(peak.observed)}</p>
          </div>
          {parts.map((part) => (
            <div className="border-line rounded border p-3" key={part.name}>
              <p className="text-muted text-[10px] tracking-wide uppercase">{part.name}</p>
              <p className="mt-1 text-xl font-semibold" style={{ color: part.color }}>
                {number(part.value)}
              </p>
            </div>
          ))}
        </div>
        <p className="text-muted mt-3 font-mono text-[11px]">
          {number(peak.observed)} observed = {number(peak.explained_base)} base +{' '}
          {number(peak.explained_event)} event + {number(peak.residual)} residual
        </p>
      </div>
      <FrameTable frames={decomposition.frames} />
    </div>
  );
}

function FrameTable({ frames }: { frames: DecompFrame[] }) {
  const recent = frames.slice(-8);
  return (
    <details className="border-line bg-sidebar rounded-lg border p-4">
      <summary className="cursor-pointer text-xs font-medium">
        Inspect {frames.length} full-resolution frame{frames.length === 1 ? '' : 's'}
      </summary>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[42rem] text-left text-xs">
          <thead className="text-muted">
            <tr>
              <th className="pb-2">UTC</th>
              <th className="pb-2">Observed</th>
              <th className="pb-2">Base</th>
              <th className="pb-2">Event</th>
              <th className="pb-2">Residual</th>
              <th className="pb-2">Residual score</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((frame) => (
              <tr className="border-line border-t" key={frame.frame_id}>
                <td className="py-2">{timestamp(frame.ts)}</td>
                <td>{number(frame.observed)}</td>
                <td>{number(frame.explained_base)}</td>
                <td>{number(frame.explained_event)}</td>
                <td className="font-medium" style={{ color: 'var(--color-residual)' }}>
                  {number(frame.residual)}
                </td>
                <td>{Math.round(frame.residual_score * 100)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function Proof({ detail }: { detail: IncidentDetail }) {
  const { audience } = useAudience();
  const verdict: VerdictClass | 'UNCLASSIFIED' =
    detail.verdict.verdict_class ?? 'UNCLASSIFIED';
  const guards = detail.action_log.decision.guards_applied ?? [];
  return (
    <article className="flex flex-col gap-10">
      <header className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="bg-accent-soft text-ink rounded px-2 py-1 font-medium tracking-wide uppercase">
            {title(verdict)}
          </span>
          <span className="border-line rounded border px-2 py-1">
            {detail.provenance.telemetry} telemetry
          </span>
          <span className="border-line rounded border px-2 py-1">
            {detail.provenance.stimulus} stimulus
          </span>
          <span className="border-line rounded border px-2 py-1">
            {detail.provenance.mode}
          </span>
          <span className="text-muted">{title(detail.state)} · {title(detail.severity)}</span>
        </div>
        <div>
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">
            {title(verdict)} incident
          </h1>
          <p className="text-muted mt-2 font-mono text-xs">{detail.incident_id}</p>
        </div>
        <dl className="text-body grid gap-2 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-muted">Window</dt>
            <dd>{timestamp(detail.opened_at)} → {timestamp(detail.updated_at)} UTC</dd>
          </div>
          <div>
            <dt className="text-muted">Services</dt>
            <dd>{detail.services.join(' · ')}</dd>
          </div>
          <div>
            <dt className="text-muted">Collapsed origin</dt>
            <dd>
              {detail.origin_service === null
                ? 'Not confirmed'
                : `${detail.origin_service} · ${Math.round((detail.origin_confidence ?? 0) * 100)}% rule support`}
            </dd>
          </div>
        </dl>
        <VerdictDistribution detail={detail} />
        <div className="border-line bg-sidebar rounded-lg border p-3 text-xs">
          <strong>
            {detail.verdict.calibration.status === 'calibrated' &&
            detail.verdict.calibration.value !== null
              ? `${Math.round(detail.verdict.calibration.value * 100)}% calibrated confidence`
              : 'Calibration insufficient'}
          </strong>
          <p className="text-muted mt-1">{detail.verdict.calibration.note}</p>
        </div>
      </header>

      <Section
        id="why-this-answer"
        title="Why this answer"
        description="The decision in plain language, followed by alternatives ruled out by measured evidence."
      >
        <div className="border-line bg-raised rounded-lg border p-5">
          <p className="text-body leading-7">{detail.reason}</p>
          {detail.verdict.reason_subtype !== null && (
            <p className="text-muted mt-2 text-xs">
              Reason subtype · {title(detail.verdict.reason_subtype)}
            </p>
          )}
        </div>
        {detail.rejected_alternatives.length > 0 && (
          <ul className="grid gap-2">
            {detail.rejected_alternatives.map((alternative) => (
              <li
                className="border-line bg-sidebar rounded-lg border p-4 text-sm"
                key={alternative.verdict_class}
              >
                <strong>Not {title(alternative.verdict_class)}</strong>
                <p className="text-muted mt-1">{alternative.reason}</p>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {audience === 'exec' ? (
        <aside className="border-line bg-sidebar rounded-lg border p-4 text-sm">
          <strong>Exec view ends at the decision.</strong>
          <p className="text-muted mt-1">
            Switch to Technical to inspect decomposition, evidence, causal collapse, verifier
            checks, and the immutable action log from this same snapshot.
          </p>
        </aside>
      ) : (
        <>
          <Section
            id="surge-decomposition"
            title="Surge decomposition"
            description="The event may explain volume. The residual is what survived that explanation."
          >
            <DecompositionProof detail={detail} />
          </Section>

          <Section
            id="evidence-chain"
            title="Evidence chain"
            description="Independent axes with the measured value, its baseline, direction, and contribution."
          >
            {detail.evidence.length === 0 ? (
              <p className="border-line bg-sidebar text-muted rounded-lg border p-4 text-sm">
                No independently scored evidence items were attached to this revision.
              </p>
            ) : (
              <ol className="grid gap-3">
                {detail.evidence.map((evidence, index) => (
                  <li
                    className="border-line bg-raised grid gap-3 rounded-lg border p-4 sm:grid-cols-[auto_1fr]"
                    key={`${evidence.assessment_id}-${evidence.feature}`}
                  >
                    <span className="bg-accent-soft text-ink flex size-8 items-center justify-center rounded-full text-xs font-semibold">
                      {index + 1}
                    </span>
                    <div>
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <strong>{title(evidence.axis)}</strong>
                        <code>{evidence.feature}</code>
                        <span className="text-muted">{evidence.symptom_kinds.map(title).join(' · ')}</span>
                      </div>
                      <p className="text-body mt-2 text-sm">
                        {number(evidence.value)} vs {number(evidence.baseline)} baseline ·{' '}
                        {title(evidence.direction)} · {Math.round(evidence.contribution * 100)}%
                        contribution
                      </p>
                      <p className="text-muted mt-1 text-xs">{evidence.note}</p>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Section>

          <Section
            id="causal-collapse"
            title="Causal collapse"
            description="Muted edges are topology. Active edges require member episode evidence and point from cause to effect."
          >
            <CausalGraphView graph={detail.causal_graph} />
          </Section>

          <Section
            id="deterministic-verification"
            title="Deterministic verification"
            description="All four non-LLM checks run against telemetry and committed topology before action."
          >
            {!detail.verification.confirmed && (
              <div className="border-warn text-warn rounded-lg border p-4 text-sm font-medium">
                PENDING REVIEW — at least one deterministic check failed.
              </div>
            )}
            <ul className="grid gap-3 sm:grid-cols-2">
              {detail.verification.checks.map((check) => (
                <li className="border-line bg-raised rounded-lg border p-4" key={check.name}>
                  <div className="flex items-center justify-between gap-2 text-xs">
                    <strong>{title(check.name)}</strong>
                    <span
                      className={
                        check.outcome === 'FAILED'
                          ? 'text-bad'
                          : check.outcome === 'BOOTSTRAP'
                            ? 'text-warn'
                            : 'text-ok'
                      }
                    >
                      {title(check.outcome)}
                    </span>
                  </div>
                  <p className="text-muted mt-2 text-xs">{check.detail}</p>
                </li>
              ))}
            </ul>
          </Section>

          <Section
            id="action-log"
            title="Action log"
            description="The policy decision and every attached immutable audit record. No UI state is treated as an effect."
          >
            <div className="border-line bg-raised rounded-lg border p-4">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <strong>{title(detail.action_log.decision.action)}</strong>
                <span className="text-muted">Rule {detail.action_log.decision.rule_id}</span>
                {detail.action_log.decision.target_service !== null && (
                  <span className="border-line rounded border px-2 py-1">
                    Target {detail.action_log.decision.target_service}
                  </span>
                )}
              </div>
              <p className="text-body mt-2 text-sm">{detail.action_log.decision.reason}</p>
              <p className="text-muted mt-2 text-xs">
                Guards ·{' '}
                {guards.length > 0
                  ? guards.join(' · ')
                  : 'none recorded'}
              </p>
            </div>
            {detail.action_log.entries.length === 0 ? (
              <p className="border-line bg-sidebar text-muted rounded-lg border p-4 text-sm">
                {detail.action_log.detail}
              </p>
            ) : (
              <ol className="border-line divide-line divide-y rounded-lg border">
                {detail.action_log.entries.map((entry) => (
                  <li className="bg-sidebar p-4" key={entry.entry_id}>
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <strong>{title(entry.kind)}</strong>
                      <span className="border-line rounded border px-2 py-1">{entry.honesty}</span>
                      <span className="text-muted">#{entry.sequence} · {timestamp(entry.ts)} UTC</span>
                    </div>
                    <p className="text-body mt-2 text-sm">{entry.summary}</p>
                  </li>
                ))}
              </ol>
            )}
            <ActionControlPanel incidentId={detail.incident_id} />
            <aside className="border-line bg-sidebar rounded-lg border p-4 text-xs">
              <strong>Code localization is not inferred.</strong>
              <p className="text-muted mt-1">
                Service-to-file evidence remains a Phase 7 product and is unavailable here until
                the real trace, change-ledger, and git verification paths produce it.
              </p>
            </aside>
          </Section>
        </>
      )}
    </article>
  );
}

export function IncidentDetailPage() {
  const { incidentId } = useParams();
  const { credential, clearCredential } = useOperatorCredential();
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const refetchQueued = useRef(false);

  const refetch = useCallback((): Promise<void> => {
    if (incidentId === undefined) {
      setLoad({ state: 'error', detail: 'The route did not name an incident.' });
      return Promise.resolve();
    }
    if (inFlight.current !== null) {
      refetchQueued.current = true;
      return inFlight.current;
    }
    const requestController = new AbortController();
    controller.current = requestController;
    const request = fetchIncidentDetail(incidentId, credential, requestController.signal)
      .then((response) => {
        if (!requestController.signal.aborted) setLoad({ state: 'ready', response });
      })
      .catch((error: unknown) => {
        if (!requestController.signal.aborted && error instanceof CredentialRequiredError) {
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
  }, [clearCredential, credential, incidentId]);

  useSnapshotInvalidation(INCIDENT_RESOURCES, refetch);
  useEffect(() => {
    setLoad({ state: 'loading' });
    void refetch();
    return () => controller.current?.abort();
  }, [refetch]);

  if (load.state === 'loading') {
    return (
      <p className="text-muted text-sm" role="status">
        Reading the incident proof…
      </p>
    );
  }
  if (load.state === 'locked') {
    return <OperatorCredentialPrompt detail={load.detail} />;
  }
  if (load.state === 'error') {
    return (
      <div className="flex flex-col gap-3" role="alert">
        <h1 className="text-2xl font-semibold">Incident proof unavailable</h1>
        <p className="text-bad text-sm">{load.detail}</p>
        <Link className="text-accent text-sm underline" to="/command#live-incidents">
          Return to live incidents
        </Link>
      </div>
    );
  }
  if (load.response.detail === null) {
    return (
      <div className="flex flex-col gap-3">
        <h1 className="text-2xl font-semibold">
          {load.response.status === 'not_found' ? 'Incident proof not found' : 'Incident proof unavailable'}
        </h1>
        <p className="text-muted text-sm">{load.response.message}</p>
        <p className="text-muted text-xs">
          An absent proof is not evidence that the system was quiet.
        </p>
        <Link className="text-accent text-sm underline" to="/command#live-incidents">
          Return to live incidents
        </Link>
      </div>
    );
  }
  return <Proof detail={load.response.detail} />;
}
