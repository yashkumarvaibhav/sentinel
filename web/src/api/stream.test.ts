import { describe, expect, it } from 'vitest';

import { parseSnapshotInvalidation, STREAM_EVENT_NAME } from '@/api/stream';

const VALID = JSON.stringify({
  event_id: 'event-1',
  ts: '2026-07-26T00:00:00Z',
  kind: 'snapshot.invalidate',
  resources: ['health'],
});

describe('parseSnapshotInvalidation', () => {
  it('accepts the checked-in public contract shape', () => {
    expect(parseSnapshotInvalidation(VALID)).toEqual({
      event_id: 'event-1',
      ts: '2026-07-26T00:00:00Z',
      kind: STREAM_EVENT_NAME,
      resources: ['health'],
    });
  });

  it.each([
    '{}',
    '{"event_id":"x","ts":"not-a-date","kind":"snapshot.invalidate","resources":["health"]}',
    '{"event_id":"x","ts":"2026-07-26T00:00:00Z","kind":"other","resources":["health"]}',
    '{"event_id":"x","ts":"2026-07-26T00:00:00Z","kind":"snapshot.invalidate","resources":[]}',
    '{"event_id":"x","ts":"2026-07-26T00:00:00Z","kind":"snapshot.invalidate","resources":["health","health"]}',
    '{"event_id":"x","ts":"2026-07-26T00:00:00Z","kind":"snapshot.invalidate","resources":["all","health"]}',
  ])('rejects malformed or ambiguous event data: %s', (raw) => {
    expect(() => parseSnapshotInvalidation(raw)).toThrow(/stream event/i);
  });
});
