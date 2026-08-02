import { useEffect, useState } from 'react';

import { fetchKpis } from '@/api/kpis';
import type { KpiKey, KpiMetric, KpiResponse, ScoreHeadline } from '@/contracts/types';
import { useAudience } from '@/shell/useAudience';
import { Chip, HonestyChip } from '@/ui/Chip';
import { SkeletonText } from '@/ui/Skeleton';

type Load =
  | { state: 'loading' }
  | { state: 'ok'; response: KpiResponse }
  | { state: 'error'; detail: string };

const EXEC_LABELS: Record<KpiKey, string> = {
  detection_latency: 'How fast we notice',
  autonomous_mttr: 'How fast automation restores',
  quiet_day_false_acts: 'Safe on quiet days',
  protected_cohort_integrity: 'Protected users unaffected',
};

function formatValue(metric: KpiMetric): string {
  if (metric.status === 'insufficient' || metric.value === null) return 'Insufficient evidence';
  if (metric.unit === 'seconds') return `${metric.value.toFixed(1)}s`;
  if (metric.unit === 'ratio') return `${Math.round(metric.value * 100)}%`;
  return `${metric.value.toLocaleString()} ${metric.unit}`;
}

function execSummary(metric: KpiMetric): string {
  if (metric.status === 'insufficient' || metric.value === null) {
    return metric.window.description;
  }
  if (metric.key === 'detection_latency') {
    return `95% of matched symptoms were noticed within ${metric.value.toFixed(1)}s.`;
  }
  return metric.definition;
}

function KpiCard({ metric, exec }: { metric: KpiMetric; exec: boolean }) {
  return (
    <article className="border-line bg-raised flex min-w-0 flex-col gap-3 rounded-lg border p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="eyebrow font-sans">
          {exec ? EXEC_LABELS[metric.key] : metric.label}
        </h3>
        {/* "Measured" and "Insufficient" are the two most consequential words
            on this screen, so neither is left to colour: each carries its own
            icon, and the insufficient one is a dashed circle rather than an
            error mark — an unmeasured metric is a gap in the telemetry, not a
            fault in the platform. */}
        {metric.status === 'ok' ? (
          <Chip tone="healthy">Measured</Chip>
        ) : (
          <Chip tone="neutral">Insufficient</Chip>
        )}
      </div>

      <p className={metric.status === 'ok' ? 'text-2xl font-semibold' : 'text-muted text-base'}>
        {formatValue(metric)}
      </p>

      <p className="text-body text-xs leading-5">
        {exec ? execSummary(metric) : metric.definition}
      </p>

      <dl className="border-line text-muted mt-auto grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 border-t pt-3 text-[11px]">
        <dt>Window</dt>
        <dd className="text-right">{metric.window.description}</dd>
        <dt>Samples</dt>
        <dd className="text-right">
          {metric.sample_count.toLocaleString()} {metric.sample_count === 1 ? 'sample' : 'samples'}
        </dd>
        {!exec && (
          <>
            <dt>Source</dt>
            <dd className="truncate text-right" title={metric.provenance}>
              {metric.provenance}
            </dd>
          </>
        )}
      </dl>
    </article>
  );
}

function formatHeadline(metric: ScoreHeadline): string {
  if (metric.status === 'insufficient' || metric.value === null) {
    return `${metric.label} insufficient`;
  }
  if (metric.unit === 'ratio') return `${metric.label} ${Math.round(metric.value * 100)}%`;
  if (metric.unit === 'seconds') return `${metric.label} ${metric.value.toFixed(1)}s`;
  return `${metric.label} ${metric.value.toLocaleString()}`;
}

function ScoreProofChip({ response }: { response: KpiResponse }) {
  const proof = response.latest_score_proof;
  if (proof === null) {
    return (
      <p className="text-warn text-xs" role="status">
        Latest score proof unavailable. {response.detail}
      </p>
    );
  }
  const latestCapture = proof.capture_ids.at(-1);
  const proofDate = new Intl.DateTimeFormat('en', {
    dateStyle: 'medium',
    timeZone: 'UTC',
  }).format(new Date(proof.evidence_end));

  return (
    <aside className="border-line bg-sidebar flex flex-col gap-3 rounded-lg border p-4 lg:flex-row lg:items-center">
      <div className="flex flex-wrap items-center gap-2 lg:shrink-0 lg:flex-nowrap">
        <strong className="text-xs tracking-wide uppercase">
          Held-out proof · {proof.gate_status}
        </strong>
        {/* Read off the proof rather than written in: `api/kpis.ts` already
            refuses a proof whose honesty is anything else, so these two agree
            with the guard by construction instead of by coincidence. */}
        <HonestyChip kind={proof.telemetry_honesty} />
        <HonestyChip kind={proof.stimulus_honesty} />
      </div>
      <p className="text-muted text-xs">
        {proofDate} · {proof.capture_ids.length} captures · latest {latestCapture}
      </p>
      <ul className="text-body flex flex-wrap gap-x-4 gap-y-1 text-xs lg:ml-auto">
        {proof.headline_metrics.map((metric) => (
          <li key={metric.key}>{formatHeadline(metric)}</li>
        ))}
      </ul>
    </aside>
  );
}

export function KpiStrip() {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const { audience } = useAudience();

  useEffect(() => {
    const controller = new AbortController();
    fetchKpis(controller.signal)
      .then((response) => setLoad({ state: 'ok', response }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ state: 'error', detail: error instanceof Error ? error.message : String(error) });
      });
    return () => controller.abort();
  }, []);

  return (
    <section className="flex flex-col gap-4" aria-labelledby="reliability-kpis">
      <div>
        <h2 id="reliability-kpis" className="font-serif text-base">
          Reliability proof
        </h2>
        <p className="text-muted mt-1 text-xs">
          Every number names its evidence window and sample count. Missing evidence stays missing.
        </p>
      </div>

      {load.state === 'loading' && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <div key={index} className="border-line bg-raised rounded-lg border p-4">
              <SkeletonText
                lines={4}
                label={index === 0 ? 'Reading reliability evidence…' : ''}
              />
            </div>
          ))}
        </div>
      )}
      {load.state === 'error' && (
        <p className="text-bad text-sm" role="alert">
          Could not validate reliability evidence: {load.detail}
        </p>
      )}
      {load.state === 'ok' && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {load.response.metrics.map((metric) => (
              <KpiCard key={metric.key} metric={metric} exec={audience === 'exec'} />
            ))}
          </div>
          <ScoreProofChip response={load.response} />
        </>
      )}
    </section>
  );
}
