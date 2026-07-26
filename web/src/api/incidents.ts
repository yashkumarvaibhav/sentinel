import type {
  ActionStatus,
  DecisionAction,
  EvidenceDirection,
  IncidentActionState,
  IncidentConfidence,
  IncidentConfidenceStatus,
  IncidentEvidenceValue,
  IncidentFeedItem,
  IncidentFeedResponse,
  IncidentSeverity,
  IncidentState,
  VerdictClass,
} from '@/contracts/types';

const STATES = new Set<IncidentState>(['OPEN', 'MITIGATING', 'MONITORING', 'RESOLVED']);
const SEVERITIES = new Set<IncidentSeverity>(['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']);
const VERDICTS = new Set<VerdictClass>([
  'EXPECTED_EVENT',
  'ATTACK',
  'OPERATIONAL_FAULT',
  'CODE_CONFIG_FAULT',
  'COMBINATION',
]);
const DECISIONS = new Set<DecisionAction>([
  'SUPPRESS',
  'ALERT',
  'ACT',
  'ESCALATE_TO_HUMAN',
  'AUTO_CONTAIN_THEN_ESCALATE',
]);
const EFFECTS = new Set<ActionStatus>([
  'SIMULATED',
  'APPLIED',
  'VERIFIED',
  'REVERTED',
  'FAILED',
]);
const DIRECTIONS = new Set<EvidenceDirection>([
  'ABOVE_BASELINE',
  'BELOW_BASELINE',
  'AT_BASELINE',
]);

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`${name} must be non-empty text`);
  }
  return value;
}

function time(value: unknown, name: string): string {
  const rendered = text(value, name);
  if (!/(?:Z|\+00:00)$/.test(rendered) || !Number.isFinite(Date.parse(rendered))) {
    throw new Error(`${name} must be a UTC timestamp`);
  }
  return rendered;
}

function finite(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${name} must be finite`);
  }
  return value;
}

function optionalText(value: unknown, name: string): string | null {
  return value === null ? null : text(value, name);
}

function member<T extends string>(value: unknown, values: Set<T>, name: string): T {
  if (typeof value !== 'string' || !values.has(value as T)) {
    throw new Error(`${name} is unknown`);
  }
  return value as T;
}

function parseEvidence(value: unknown): IncidentEvidenceValue {
  const body = record(value, 'incident evidence');
  const direction = member(body.direction, DIRECTIONS, 'incident evidence direction');
  const measured = finite(body.value, 'incident evidence value');
  const baseline = finite(body.baseline, 'incident evidence baseline');
  if (
    (measured > baseline && direction !== 'ABOVE_BASELINE') ||
    (measured < baseline && direction !== 'BELOW_BASELINE') ||
    (measured === baseline && direction !== 'AT_BASELINE')
  ) {
    throw new Error('incident evidence direction contradicts its values');
  }
  return {
    feature: text(body.feature, 'incident evidence feature'),
    value: measured,
    baseline,
    direction,
    note: text(body.note, 'incident evidence note'),
  };
}

function parseAction(value: unknown): IncidentActionState {
  const body = record(value, 'incident action');
  return {
    decision_action: member(body.decision_action, DECISIONS, 'incident decision action'),
    effect_status:
      body.effect_status === null
        ? null
        : member(body.effect_status, EFFECTS, 'incident effect status'),
    detail: text(body.detail, 'incident action detail'),
  };
}

function parseConfidence(value: unknown): IncidentConfidence {
  const body = record(value, 'incident confidence');
  const status = member(
    body.status,
    new Set<IncidentConfidenceStatus>(['calibrated', 'insufficient']),
    'incident confidence status',
  );
  const confidence = body.value === null ? null : finite(body.value, 'incident confidence');
  if (confidence !== null && (confidence < 0 || confidence > 1)) {
    throw new Error('incident confidence must be within [0, 1]');
  }
  if (
    (status === 'calibrated' && confidence === null) ||
    (status === 'insufficient' && confidence !== null)
  ) {
    throw new Error('incident confidence value contradicts its status');
  }
  return {
    status,
    value: confidence,
    note: text(body.note, 'incident confidence note'),
  };
}

function parseItem(value: unknown): IncidentFeedItem {
  const body = record(value, 'incident feed item');
  if (!Array.isArray(body.services) || body.services.length === 0) {
    throw new Error('incident services must be non-empty');
  }
  if (!Array.isArray(body.evidence) || body.evidence.length > 2) {
    throw new Error('incident evidence must contain at most two values');
  }
  const evidence: IncidentFeedItem['evidence'] =
    body.evidence.length === 0
      ? []
      : body.evidence.length === 1
        ? [parseEvidence(body.evidence[0])]
        : [parseEvidence(body.evidence[0]), parseEvidence(body.evidence[1])];
  const services: [string, ...string[]] = [
    text(body.services[0], 'incident service'),
    ...body.services.slice(1).map((service) => text(service, 'incident service')),
  ];
  if (new Set(services).size !== services.length) {
    throw new Error('incident services must be unique');
  }
  const origin = optionalText(body.origin_service, 'incident origin');
  if (origin !== null && !services.includes(origin)) {
    throw new Error('incident origin must be one of its services');
  }
  const verdict =
    body.verdict_class === null
      ? null
      : member(body.verdict_class, VERDICTS, 'incident verdict');
  const action = parseAction(body.action);
  const muted = body.muted;
  if (typeof muted !== 'boolean') throw new Error('incident muted state must be boolean');
  const explanation = optionalText(body.explanation, 'incident explanation');
  if (muted !== (explanation !== null)) {
    throw new Error('muted incident and explanation must agree');
  }
  if (muted && (verdict !== 'EXPECTED_EVENT' || action.decision_action !== 'SUPPRESS')) {
    throw new Error('only a suppressed expected event may be muted');
  }
  if (body.honesty !== 'REAL' && body.honesty !== 'SIMULATED') {
    throw new Error('incident honesty is unknown');
  }
  return {
    incident_id: text(body.incident_id, 'incident id'),
    opened_at: time(body.opened_at, 'incident opening'),
    updated_at: time(body.updated_at, 'incident update'),
    state: member(body.state, STATES, 'incident state'),
    severity: member(body.severity, SEVERITIES, 'incident severity'),
    services,
    origin_service: origin,
    verdict_class: verdict,
    reason: text(body.reason, 'incident reason'),
    evidence,
    action,
    confidence: parseConfidence(body.confidence),
    muted,
    explanation,
    honesty: body.honesty,
  };
}

export function parseIncidentFeedResponse(value: unknown): IncidentFeedResponse {
  const body = record(value, 'incident feed response');
  if (body.status !== 'ready' && body.status !== 'degraded') {
    throw new Error('incident feed status is unknown');
  }
  if (!Array.isArray(body.incidents)) throw new Error('incidents must be an array');
  if (
    typeof body.limit !== 'number' ||
    !Number.isInteger(body.limit) ||
    body.limit < 1 ||
    body.limit > 50
  ) {
    throw new Error('incident feed limit must be within [1, 50]');
  }
  const incidents = body.incidents.map(parseItem);
  if (
    typeof body.count !== 'number' ||
    !Number.isInteger(body.count) ||
    body.count !== incidents.length ||
    body.count > body.limit
  ) {
    throw new Error('incident feed count contradicts its items or limit');
  }
  const detail = body.detail === null ? null : text(body.detail, 'incident feed detail');
  if (
    (body.status === 'ready' && detail !== null) ||
    (body.status === 'degraded' && (detail === null || incidents.length > 0))
  ) {
    throw new Error('incident feed status contradicts its detail or items');
  }
  return {
    status: body.status,
    incidents,
    count: body.count,
    limit: body.limit,
    detail,
  };
}

export async function fetchIncidents(signal?: AbortSignal): Promise<IncidentFeedResponse> {
  const response = await fetch('/api/incidents?limit=20', signal ? { signal } : {});
  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`incident request failed: ${response.status}`);
  }
  return parseIncidentFeedResponse(await response.json());
}
