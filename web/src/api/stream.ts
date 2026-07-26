import type { SnapshotInvalidation, SnapshotResource } from '@/contracts/types';

export const STREAM_EVENT_NAME = 'snapshot.invalidate';
export const STREAM_URL = '/stream/events';

const RESOURCES = new Set<SnapshotResource>([
  'all',
  'health',
  'decomposition',
  'incidents',
  'actions',
  'audit',
]);
const FIELDS = new Set(['event_id', 'ts', 'kind', 'resources']);

function fail(detail: string): never {
  throw new Error(`invalid stream event: ${detail}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Validate the generated public contract at the untrusted browser boundary.
 *
 * A TypeScript assertion would only document what the server was meant to
 * send. This check proves what arrived before any invalidation can trigger
 * network work or alter a connection indicator.
 */
export function parseSnapshotInvalidation(raw: string): SnapshotInvalidation {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return fail('data is not JSON');
  }
  if (!isRecord(value)) return fail('data is not an object');
  if (Object.keys(value).some((field) => !FIELDS.has(field))) {
    return fail('data contains an unknown field');
  }
  if (
    typeof value.event_id !== 'string' ||
    value.event_id.trim().length === 0 ||
    value.event_id.length > 255
  ) {
    return fail('event_id is not a bounded identifier');
  }
  if (
    typeof value.ts !== 'string' ||
    !/(?:Z|\+00:00)$/.test(value.ts) ||
    !Number.isFinite(Date.parse(value.ts))
  ) {
    return fail('ts is not a UTC timestamp');
  }
  if (value.kind !== STREAM_EVENT_NAME) return fail('kind is unknown');
  if (
    !Array.isArray(value.resources) ||
    value.resources.length === 0 ||
    !value.resources.every(
      (resource): resource is SnapshotResource =>
        typeof resource === 'string' && RESOURCES.has(resource as SnapshotResource),
    )
  ) {
    return fail('resources are empty or unknown');
  }
  if (new Set(value.resources).size !== value.resources.length) {
    return fail('resources contain duplicates');
  }
  if (value.resources.includes('all') && value.resources.length !== 1) {
    return fail('all must be the only resource');
  }

  return {
    event_id: value.event_id,
    ts: value.ts,
    kind: STREAM_EVENT_NAME,
    resources: value.resources as SnapshotInvalidation['resources'],
  };
}
