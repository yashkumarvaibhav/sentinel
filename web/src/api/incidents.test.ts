import { describe, expect, it } from 'vitest';

import { parseIncidentFeedResponse } from '@/api/incidents';

const ITEM = {
  incident_id: 'incident-1',
  opened_at: '2026-07-26T02:58:00Z',
  updated_at: '2026-07-26T03:00:00Z',
  state: 'OPEN',
  severity: 'HIGH',
  services: ['frontend'],
  origin_service: 'frontend',
  verdict_class: 'ATTACK',
  reason: 'Evidence supports an attack.',
  evidence: [],
  action: {
    decision_action: 'ALERT',
    effect_status: null,
    detail: 'Alert raised; no production effect was requested.',
  },
  confidence: {
    status: 'insufficient',
    value: null,
    note: 'No calibrated runtime confidence is attached.',
  },
  muted: false,
  explanation: null,
  honesty: 'REAL',
};

function response(item: unknown = ITEM) {
  return {
    status: 'ready',
    incidents: [item],
    count: 1,
    limit: 20,
    detail: null,
  };
}

describe('parseIncidentFeedResponse', () => {
  it('accepts the strict evidence-only snapshot', () => {
    expect(parseIncidentFeedResponse(response()).incidents[0]).toMatchObject({
      incident_id: 'incident-1',
      honesty: 'REAL',
    });
  });

  it('rejects raw numeric confidence disguised as calibrated', () => {
    const item = {
      ...ITEM,
      confidence: { ...ITEM.confidence, status: 'insufficient', value: 0.91 },
    };

    expect(() => parseIncidentFeedResponse(response(item))).toThrow(/confidence value/i);
  });

  it('rejects a muted hostile incident with an event explanation', () => {
    const item = { ...ITEM, muted: true, explanation: 'Explained by Cup final' };

    expect(() => parseIncidentFeedResponse(response(item))).toThrow(/suppressed expected event/i);
  });
});
