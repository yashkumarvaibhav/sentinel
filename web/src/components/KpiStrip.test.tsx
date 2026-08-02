import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { KpiStrip } from '@/components/KpiStrip';
import { useAudience } from '@/shell/useAudience';

const RESPONSE = {
  status: 'ready',
  detail: null,
  metrics: [
    {
      key: 'detection_latency',
      label: 'Detection latency',
      definition: 'P95 time from labeled symptom onset to a matching runtime episode.',
      status: 'ok',
      value: 181.6,
      unit: 'seconds',
      window: {
        start: '2026-07-23T12:00:00Z',
        end: '2026-07-23T12:20:00Z',
        description: 'latest held-out score across 4 captures',
      },
      sample_count: 20,
      provenance: 'phase-2-held-out-symptoms · docs/reports/phase-2-held-out-score.md',
    },
    {
      key: 'autonomous_mttr',
      label: 'Autonomous MTTR',
      definition: 'Time from an autonomous action to verified SLO recovery.',
      status: 'ok',
      value: 16.766534,
      unit: 'seconds',
      window: {
        start: '2026-08-02T09:14:29Z',
        end: '2026-08-02T09:14:46Z',
        description: 'contained real-testbed action recovery across 1 run',
      },
      sample_count: 1,
      provenance: 'phase-6-contained-action-recovery · report',
    },
    {
      key: 'quiet_day_false_acts',
      label: 'Quiet-day false acts',
      definition: 'Autonomous actions on held-out quiet-day evidence.',
      status: 'ok',
      value: 0,
      unit: 'actions',
      window: {
        start: '2026-07-21T13:14:39Z',
        end: '2026-07-21T13:18:09Z',
        description: 'held-out quiet-day decision replay across 2 captures',
      },
      sample_count: 2,
      provenance: 'phase-6-held-out-quiet-actions · report',
    },
    {
      key: 'protected_cohort_integrity',
      label: 'Protected-cohort integrity',
      definition: 'Protected requests inside their SLO during mitigation.',
      status: 'insufficient',
      value: null,
      unit: 'ratio',
      window: {
        start: null,
        end: null,
        description: 'No protected-cohort telemetry reader is attached.',
      },
      sample_count: 0,
      provenance: 'No protected-cohort telemetry reader is attached.',
    },
  ],
  latest_score_proof: {
    version: 1,
    proof_id: 'phase-2-held-out-symptoms',
    gate_status: 'pass',
    evidence_start: '2026-07-23T12:00:00Z',
    evidence_end: '2026-07-23T12:20:00Z',
    telemetry_honesty: 'REAL',
    stimulus_honesty: 'SIMULATED',
    seed_purpose: 'held_out',
    capture_ids: [
      'phase2-cascade-9403-v1',
      'phase2-cascade-9421-v1',
      'phase2-combo-9439-v1',
      'phase2-combo-9457-v1',
    ],
    config_fingerprint: 'a'.repeat(64),
    report_path: 'docs/reports/phase-2-held-out-score.md',
    headline_metrics: [
      {
        key: 'minimum_symptom_recall',
        label: 'Minimum symptom recall',
        status: 'ok',
        value: 1,
        unit: 'ratio',
        sample_count: 7,
      },
      {
        key: 'detection_latency_p95_seconds',
        label: 'Detection latency p95',
        status: 'ok',
        value: 181.6,
        unit: 'seconds',
        sample_count: 20,
      },
    ],
  },
};

function serve(body: unknown = RESPONSE, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status }))),
  );
}

function AudienceControl() {
  const { audience, toggle } = useAudience();
  return <button onClick={toggle}>Audience: {audience}</button>;
}

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe('KpiStrip', () => {
  it('shows three measured values and keeps the one unavailable metric distinct', async () => {
    serve();

    render(<KpiStrip />);

    expect(await screen.findByText('181.6s')).toBeInTheDocument();
    expect(screen.getByText('16.8s')).toBeInTheDocument();
    expect(screen.getByText('0 actions')).toBeInTheDocument();
    expect(screen.getAllByText('Insufficient evidence')).toHaveLength(1);
    expect(screen.queryByText('0s')).not.toBeInTheDocument();
    expect(screen.getByText(/20 samples/i)).toBeInTheDocument();
    expect(screen.getByText(/2 samples · held-out quiet-day/i)).toBeInTheDocument();
  });

  it('shows the generated held-out proof with honesty and capture provenance', async () => {
    serve();

    render(<KpiStrip />);

    expect(await screen.findByText(/held-out proof · pass/i)).toBeInTheDocument();
    expect(screen.getByText('REAL')).toBeInTheDocument();
    expect(screen.getByText('SIMULATED')).toBeInTheDocument();
    expect(screen.getByText(/phase2-combo-9457-v1/i)).toBeInTheDocument();
    expect(screen.getByText(/minimum symptom recall 100%/i)).toBeInTheDocument();
  });

  it('switches both consumers to the exec rendering in the same tab', async () => {
    serve();
    const user = userEvent.setup();

    render(
      <>
        <AudienceControl />
        <KpiStrip />
      </>,
    );
    await screen.findByText('Detection latency');

    await user.click(screen.getByRole('button', { name: 'Audience: technical' }));

    expect(screen.getByRole('button', { name: 'Audience: exec' })).toBeInTheDocument();
    expect(screen.getByText('How fast we notice')).toBeInTheDocument();
    expect(screen.queryByText('Detection latency')).not.toBeInTheDocument();
    expect(screen.getByText(/95% of matched symptoms were noticed/i)).toBeInTheDocument();
  });

  it('keeps the typed insufficient snapshot when the artifact is unavailable', async () => {
    serve(
      {
        ...RESPONSE,
        status: 'degraded',
        detail: 'score proof unavailable',
        metrics: RESPONSE.metrics.map((metric) => ({
          ...metric,
          status: 'insufficient',
          value: null,
          sample_count: 0,
        })),
        latest_score_proof: null,
      },
      503,
    );

    render(<KpiStrip />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/score proof unavailable/i);
    });
    expect(screen.getAllByText('Insufficient evidence')).toHaveLength(4);
  });

  it('refuses malformed KPI JSON instead of trusting a server assertion', async () => {
    serve({
      ...RESPONSE,
      metrics: RESPONSE.metrics.slice(0, 3),
    });

    render(<KpiStrip />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/exactly four/i);
  });
});
