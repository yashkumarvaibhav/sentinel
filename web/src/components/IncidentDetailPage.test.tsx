import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';

import { IncidentDetailPage } from '@/components/IncidentDetailPage';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';
import { OperatorCredentialProvider } from '@/shell/OperatorCredential';

vi.mock('@xyflow/react', () => ({
  Background: () => null,
  Handle: () => null,
  MarkerType: { ArrowClosed: 'arrowclosed' },
  Panel: () => null,
  Position: { Top: 'top', Right: 'right', Bottom: 'bottom', Left: 'left' },
  ReactFlow: ({ nodes }: { nodes: Array<{ id: string; ariaLabel?: string }> }) => (
    <div aria-label="Causal topology canvas">
      {nodes.map((node) => (
        <span key={node.id}>{node.ariaLabel}</span>
      ))}
    </div>
  ),
}));

const DETAIL = {
  status: 'ready',
  detail: {
    incident_id: 'incident-proof-1',
    opened_at: '2026-07-26T04:57:00Z',
    updated_at: '2026-07-26T05:00:00Z',
    state: 'OPEN',
    severity: 'HIGH',
    services: ['checkout', 'payment'],
    origin_service: 'payment',
    origin_confidence: 0.91,
    verdict: {
      status: 'decided',
      verdict_class: 'ATTACK',
      reason_subtype: null,
      distribution: [
        { verdict_class: 'EXPECTED_EVENT', probability: 0.02 },
        { verdict_class: 'ATTACK', probability: 0.82 },
        { verdict_class: 'OPERATIONAL_FAULT', probability: 0.04 },
        { verdict_class: 'CODE_CONFIG_FAULT', probability: 0.02 },
        { verdict_class: 'COMBINATION', probability: 0.1 },
      ],
      calibration: {
        status: 'insufficient',
        value: null,
        note: 'Runtime rule confidence is not calibrated.',
      },
    },
    reason: 'Machine-regular failures survived the event explanation.',
    rejected_alternatives: [
      {
        verdict_class: 'EXPECTED_EVENT',
        reason: 'The auth-failure ratio deformed after event volume was removed.',
      },
    ],
    decomposition: {
      status: 'available',
      service: 'checkout',
      signal: 'auth.failure_ratio',
      start: '2026-07-26T04:57:00Z',
      end: '2026-07-26T05:00:00Z',
      frames: [
        {
          frame_id: 'frame-proof-1',
          observation_id: 'observation-proof-1',
          ts: '2026-07-26T04:59:00Z',
          service: 'checkout',
          signal: 'auth.failure_ratio',
          observed: 84,
          explained_base: 5,
          explained_event: 60,
          residual: 19,
          band_low: 60,
          band_high: 70,
          residual_score: 0.88,
          context_ids: ['cup-final'],
        },
      ],
      truncated: false,
      detail: 'One complete incident-window series.',
    },
    evidence: [
      {
        assessment_id: 'assessment-security-1',
        axis: 'SECURITY',
        symptom_kinds: ['RATIO_DEFORM'],
        services: ['checkout'],
        feature: 'auth.failure_ratio',
        value: 0.74,
        baseline: 0.05,
        direction: 'ABOVE_BASELINE',
        contribution: 0.82,
        note: 'Authentication failures rose against baseline.',
        evidence_refs: ['metric-auth-failure'],
      },
    ],
    causal_graph: {
      incident_id: 'incident-proof-1',
      updated_at: '2026-07-26T05:00:00Z',
      incident_state: 'OPEN',
      nodes: [
        {
          service: 'checkout',
          tier: 'application',
          criticality: 'critical',
          symptom_heat: 0.86,
          active_episode_count: 1,
          symptom_kinds: ['RATIO_DEFORM'],
          is_origin: false,
          origin_confidence: null,
          implicated: false,
          note: 'checkout carried the measured ratio symptom',
        },
        {
          service: 'payment',
          tier: 'application',
          criticality: 'critical',
          symptom_heat: 0,
          active_episode_count: 0,
          symptom_kinds: [],
          is_origin: true,
          origin_confidence: 0.91,
          implicated: true,
          note: 'payment was implicated by dependency evidence',
        },
      ],
      edges: [
        {
          source_service: 'payment',
          target_service: 'checkout',
          active: false,
          evidence_episode_ids: [],
          note: 'Committed topology only.',
        },
      ],
      origin_service: 'payment',
      origin_confidence: 0.91,
      honesty: 'REAL',
    },
    verification: {
      verification_id: 'verification-proof-1',
      ts: '2026-07-26T05:00:00Z',
      incident_id: 'incident-proof-1',
      confirmed: true,
      checks: [
        { name: 'temporal_causality', outcome: 'PASSED', detail: 'Cause preceded effect.' },
        { name: 'trace_coverage', outcome: 'PASSED', detail: 'Trace coverage passed.' },
        { name: 'dependency_validity', outcome: 'PASSED', detail: 'Dependency is committed.' },
        { name: 'memory_similarity', outcome: 'BOOTSTRAP', detail: 'No prior memory.' },
      ],
    },
    action_log: {
      decision: {
        decision_id: 'decision-proof-1',
        ts: '2026-07-26T05:00:00Z',
        incident_id: 'incident-proof-1',
        action: 'AUTO_CONTAIN_THEN_ESCALATE',
        rule_id: 'contain-verified-attack',
        reason: 'Contain the verified hostile residual and notify a person.',
        evidence_ts: '2026-07-26T05:00:00Z',
        severity: 'HIGH',
        confirmed: true,
        verification_id: 'verification-proof-1',
        requires_human_approval: false,
        verdict_class: 'ATTACK',
        verdict_id: 'verdict-proof-1',
        confidence: 0.91,
        target_service: 'payment',
        approval_reasons: [],
        escalation_reasons: ['A verified attack always pages a person.'],
        guards_applied: ['protected-service-target'],
        floors_applied: [],
        suppression: null,
      },
      entries: [],
      detail: 'No actuator records are attached.',
    },
    provenance: {
      telemetry: 'REAL',
      stimulus: 'SIMULATED',
      mode: 'LIVE',
      capture_id: null,
      seed: null,
    },
  },
  message: null,
};

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={['/incidents/incident-proof-1']}>
      <OperatorCredentialProvider>
        <SnapshotStreamProvider>
          <Routes>
            <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
          </Routes>
        </SnapshotStreamProvider>
      </OperatorCredentialProvider>
    </MemoryRouter>,
  );
}

it('renders the ordered technical proof and honest provenance', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(DETAIL), { status: 200 }))),
  );

  renderDetail();

  await waitFor(() => expect(screen.getByRole('heading', { name: /attack incident/i })).toBeVisible());
  expect(screen.getByText('REAL telemetry')).toBeVisible();
  expect(screen.getByText('SIMULATED stimulus')).toBeVisible();
  expect(screen.getByRole('heading', { name: /why this answer/i })).toBeVisible();
  expect(screen.getByRole('heading', { name: /surge decomposition/i })).toBeVisible();
  expect(screen.getByRole('heading', { name: /evidence chain/i })).toBeVisible();
  expect(screen.getByRole('heading', { name: /causal collapse/i })).toBeVisible();
  expect(screen.getByRole('heading', { name: /deterministic verification/i })).toBeVisible();
  expect(screen.getByRole('heading', { name: /action log/i })).toBeVisible();
  expect(screen.getByText(/runtime rule confidence is not calibrated/i)).toBeVisible();
});

it('makes exec mode stop after the same plain-language decision', async () => {
  window.localStorage.setItem('sentinel.audience', 'exec');
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(DETAIL), { status: 200 }))),
  );

  renderDetail();

  await waitFor(() => expect(screen.getByRole('heading', { name: /attack incident/i })).toBeVisible());
  expect(screen.getByRole('heading', { name: /why this answer/i })).toBeVisible();
  expect(screen.getByText(/exec view ends at the decision/i)).toBeVisible();
  expect(screen.queryByRole('heading', { name: /surge decomposition/i })).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /action log/i })).not.toBeInTheDocument();
});

it('does not turn a missing proof into a quiet-system claim', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            status: 'not_found',
            detail: null,
            message: 'No durable proof exists for incident incident-proof-1.',
          }),
          { status: 404 },
        ),
      ),
    ),
  );

  renderDetail();

  await waitFor(() =>
    expect(screen.getByRole('heading', { name: /incident proof not found/i })).toBeVisible(),
  );
  expect(screen.getByText(/not evidence that the system was quiet/i)).toBeVisible();
});

it('unlocks a protected direct link with a credential that stays in request memory', async () => {
  const requestUrl = (input: RequestInfo | URL): string =>
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = requestUrl(input);
    if (url.endsWith('/action')) {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            status: 'not_found',
            control: null,
            message: 'No action plan exists for this incident.',
          }),
          { status: 404 },
        ),
      );
    }
    const headers = init?.headers as Record<string, string> | undefined;
    return Promise.resolve(
      headers?.['x-sentinel-secret'] === 'proof-secret'
        ? new Response(JSON.stringify(DETAIL), { status: 200 })
        : new Response(JSON.stringify({ detail: 'secret required' }), {
            status: 401,
          }),
    );
  });
  vi.stubGlobal('fetch', fetch);

  renderDetail();
  await waitFor(() => expect(screen.getByLabelText(/operator credential/i)).toBeVisible());
  fireEvent.change(screen.getByLabelText(/operator credential/i), {
    target: { value: 'proof-secret' },
  });
  fireEvent.click(screen.getByRole('button', { name: /unlock action controls/i }));

  await waitFor(() => expect(screen.getByRole('heading', { name: /attack incident/i })).toBeVisible());
  const protectedCall = fetch.mock.calls.find(
    ([input, init]) => !requestUrl(input).endsWith('/action') && init?.headers !== undefined,
  );
  expect(protectedCall?.[1]?.headers).toEqual({
    'x-sentinel-secret': 'proof-secret',
  });
});
