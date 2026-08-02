import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LiveOperationsTape } from '@/components/LiveOperationsTape';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';

class FakeEventSource {
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): void {
    this.readyState = 2;
  }
}

const ACTIVITY = {
  in_flight: true,
  run_id: 'run-1',
  scenario_id: 'combo_night',
  mode: 'LIVE',
  state: 'RUNNING',
  started_at: new Date(Date.now() - 12_000).toISOString(),
  evidence_start_at: null,
  evidence_end_at: null,
  evidence_cursor_at: null,
  progress: 0.42,
  replay_incident: null,
  note: 'Driving the testbed.',
};

const HEALTH = {
  status: 'ready',
  degraded: [],
  components: [
    { name: 'postgres', ready: true, latency_ms: 3.2, detail: null },
    { name: 'clickhouse', ready: true, latency_ms: 12.4, detail: null },
  ],
};

const INCIDENTS = {
  status: 'ready',
  incidents: [
    {
      incident_id: 'attack-1',
      opened_at: '2026-08-02T09:00:00Z',
      updated_at: '2026-08-02T09:00:02Z',
      state: 'OPEN',
      severity: 'HIGH',
      services: ['frontend'],
      origin_service: 'frontend',
      verdict_class: 'ATTACK',
      reason: 'Hostile traffic is confirmed.',
      evidence: [],
      action: {
        decision_action: 'AUTO_CONTAIN_THEN_ESCALATE',
        effect_status: null,
        detail: 'Contain and escalate.',
      },
      confidence: { status: 'insufficient', value: null, note: 'Not calibrated.' },
      muted: false,
      explanation: null,
      honesty: 'REAL',
    },
  ],
  count: 1,
  limit: 50,
  observation: {
    status: 'WATCHING',
    last_judged_at: new Date(Date.now() - 3_000).toISOString(),
    age_seconds: 3,
    expected_within_seconds: 120,
    note: 'Live telemetry is being judged now.',
  },
  detail: null,
};

afterEach(() => vi.unstubAllGlobals());

describe('LiveOperationsTape', () => {
  it('renders changing scenario, incident, threat, freshness and plane evidence', async () => {
    vi.stubGlobal('EventSource', FakeEventSource);
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        const body = url.includes('/api/activity')
          ? ACTIVITY
          : url.includes('/api/health')
            ? HEALTH
            : INCIDENTS;
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
      }),
    );

    render(
      <SnapshotStreamProvider>
        <LiveOperationsTape />
      </SnapshotStreamProvider>,
    );

    const tape = await screen.findByTestId('live-operations-tape');
    expect(tape).toHaveTextContent(/combo_night/i);
    expect(tape).toHaveTextContent(/42%/i);
    expect(tape).toHaveTextContent(/active incidents\s*1/i);
    expect(tape).toHaveTextContent(/threats\s*1/i);
    expect(tape).toHaveTextContent(/2\/2 ready/i);
    expect(tape).toHaveTextContent(/slowest clickhouse 12ms/i);
  });
});
