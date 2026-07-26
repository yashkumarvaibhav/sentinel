import { describe, expect, it } from 'vitest';

import { parseCausalGraphResponse } from '@/api/causalGraph';

const GRAPH = {
  incident_id: 'incident-live-1',
  incident_state: 'OPEN',
  updated_at: '2026-07-26T03:00:00Z',
  honesty: 'REAL',
  origin_service: 'payment',
  origin_confidence: 0.88,
  nodes: [
    {
      service: 'checkout',
      tier: 'application',
      criticality: 'critical',
      symptom_heat: 0.81,
      active_episode_count: 1,
      symptom_kinds: ['EDGE_DEGRADED'],
      is_origin: false,
      origin_confidence: null,
      implicated: false,
      note: 'One active edge degradation episode.',
    },
    {
      service: 'payment',
      tier: 'application',
      criticality: 'critical',
      symptom_heat: 0,
      active_episode_count: 0,
      symptom_kinds: [],
      is_origin: true,
      origin_confidence: 0.88,
      implicated: true,
      note: 'Collapsed origin implicated by dependency evidence.',
    },
  ],
  edges: [
    {
      source_service: 'payment',
      target_service: 'checkout',
      active: true,
      evidence_episode_ids: ['edge-1'],
      note: 'Measured propagation from dependency to caller.',
    },
  ],
};

describe('parseCausalGraphResponse', () => {
  it('accepts one strict evidence-backed graph', () => {
    const response = parseCausalGraphResponse({
      status: 'ready',
      graph: GRAPH,
      detail: null,
    });

    expect(response.graph?.origin_service).toBe('payment');
    expect(response.graph?.edges[0]?.active).toBe(true);
  });

  it('rejects active propagation without episode evidence', () => {
    const graph = {
      ...GRAPH,
      edges: [{ ...GRAPH.edges[0], evidence_episode_ids: [] }],
    };

    expect(() =>
      parseCausalGraphResponse({ status: 'ready', graph, detail: null }),
    ).toThrow(/evidence-backed/i);
  });

  it('rejects an origin that disagrees with its node', () => {
    const graph = { ...GRAPH, origin_service: 'checkout' };

    expect(() =>
      parseCausalGraphResponse({ status: 'ready', graph, detail: null }),
    ).toThrow(/origin/i);
  });

  it('keeps a trustworthy empty snapshot distinct from degraded storage', () => {
    expect(
      parseCausalGraphResponse({
        status: 'empty',
        graph: null,
        detail: 'No current incident has an evidence-backed causal graph.',
      }).status,
    ).toBe('empty');

    expect(() =>
      parseCausalGraphResponse({
        status: 'degraded',
        graph: GRAPH,
        detail: 'Storage unavailable.',
      }),
    ).toThrow(/status/i);
  });

  it('rejects unknown fields at the public boundary', () => {
    expect(() =>
      parseCausalGraphResponse({
        status: 'ready',
        graph: { ...GRAPH, answer_key: 'payment' },
        detail: null,
      }),
    ).toThrow(/unknown field/i);
  });
});
