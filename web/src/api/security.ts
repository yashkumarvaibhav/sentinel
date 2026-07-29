import type {
  ActionControlState,
  ActionKind,
  DecompFrame,
  EpisodeStatus,
  IncidentDecomposition,
  IncidentState,
  SecurityCohort,
  SecurityFeature,
  SecurityMeasurement,
  SecurityMeasurementUnit,
  SecurityMitigation,
  SecurityResponse,
  SecuritySnapshot,
  SecurityTimelineEvent,
  SymptomKind,
} from '@/contracts/types';

const FEATURES: readonly SecurityFeature[] = [
  'PATH_ENTROPY',
  'SOURCE_ENTROPY',
  'AUTH_FAILURE_RATIO',
  'ASN_REPUTATION',
  'SESSION_ENTROPY',
  'MACHINE_TIMING',
  'PROTECTED_COHORT_INTEGRITY',
];
const COHORT_FEATURES: readonly SecurityFeature[] = [
  'SOURCE_ENTROPY',
  'AUTH_FAILURE_RATIO',
  'ASN_REPUTATION',
  'SESSION_ENTROPY',
  'MACHINE_TIMING',
];
const FEATURE_SET = new Set(FEATURES);
const UNITS = new Set<SecurityMeasurementUnit>([
  'DEFORMATION_SCORE',
  'RATIO',
  'ENTROPY',
  'REPUTATION_SCORE',
  'COEFFICIENT_OF_VARIATION',
  'INTEGRITY_RATIO',
]);
const FEATURE_UNITS: Record<SecurityFeature, ReadonlySet<SecurityMeasurementUnit>> = {
  PATH_ENTROPY: new Set(['ENTROPY', 'DEFORMATION_SCORE']),
  SOURCE_ENTROPY: new Set(['ENTROPY', 'DEFORMATION_SCORE']),
  AUTH_FAILURE_RATIO: new Set(['RATIO', 'DEFORMATION_SCORE']),
  ASN_REPUTATION: new Set(['REPUTATION_SCORE']),
  SESSION_ENTROPY: new Set(['ENTROPY', 'DEFORMATION_SCORE']),
  MACHINE_TIMING: new Set(['COEFFICIENT_OF_VARIATION', 'DEFORMATION_SCORE']),
  PROTECTED_COHORT_INTEGRITY: new Set(['INTEGRITY_RATIO']),
};
const PROBABILITY_UNITS = new Set<SecurityMeasurementUnit>([
  'DEFORMATION_SCORE',
  'RATIO',
  'REPUTATION_SCORE',
  'INTEGRITY_RATIO',
]);
const INCIDENT_STATES = new Set<IncidentState>(['OPEN', 'MITIGATING', 'MONITORING', 'RESOLVED']);
const EPISODE_STATES = new Set<EpisodeStatus>(['ACTIVE', 'CLOSED']);
const TIMELINE_KINDS = new Set<SymptomKind>(['RESIDUAL_EXCEED', 'RATIO_DEFORM']);
const ACTION_STATES = new Set<ActionControlState>([
  'AWAITING_APPROVAL',
  'APPLY_REQUESTED',
  'REJECTED',
  'APPLIED',
  'VERIFIED',
  'FAILED',
  'ROLLBACK_REQUESTED',
  'ROLLED_BACK',
  'REFUSED',
  'SIMULATED',
]);
const ACTION_KINDS = new Set<ActionKind>([
  'OBSERVE',
  'RATE_LIMIT',
  'THROTTLE',
  'SCALE',
  'FLAG_FLIP',
  'RESTART',
  'ISOLATE',
  'ROLLBACK',
]);

export class SecurityCredentialRequiredError extends Error {
  constructor() {
    super('An operator credential is required for the protected security view.');
    this.name = 'SecurityCredentialRequiredError';
  }
}

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exact(body: Record<string, unknown>, fields: readonly string[], name: string): void {
  const allowed = new Set(fields);
  const unknown = Object.keys(body).filter((key) => !allowed.has(key));
  const missing = fields.filter((key) => !(key in body));
  if (unknown.length > 0) throw new Error(`${name} has unknown field: ${unknown.sort().join(', ')}`);
  if (missing.length > 0) throw new Error(`${name} is missing field: ${missing.join(', ')}`);
}

function text(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`${name} must be non-empty text`);
  }
  return value;
}

function nullableText(value: unknown, name: string): string | null {
  return value === null ? null : text(value, name);
}

function time(value: unknown, name: string): string {
  const rendered = text(value, name);
  if (!/(?:Z|\+00:00)$/.test(rendered) || !Number.isFinite(Date.parse(rendered))) {
    throw new Error(`${name} must be a UTC timestamp`);
  }
  return rendered;
}

function nullableTime(value: unknown, name: string): string | null {
  return value === null ? null : time(value, name);
}

function finite(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${name} must be finite`);
  }
  return value;
}

function integer(value: unknown, name: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum) {
    throw new Error(`${name} must be an integer greater than or equal to ${minimum}`);
  }
  return value;
}

function probability(value: unknown, name: string): number {
  const measured = finite(value, name);
  if (measured < 0 || measured > 1) throw new Error(`${name} must be within [0, 1]`);
  return measured;
}

function bool(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${name} must be boolean`);
  return value;
}

function member<T extends string>(value: unknown, values: ReadonlySet<T>, name: string): T {
  if (typeof value !== 'string' || !values.has(value as T)) throw new Error(`${name} is unknown`);
  return value as T;
}

function uniqueTexts(value: unknown, name: string, minimum = 0): string[] {
  if (!Array.isArray(value) || value.length < minimum) {
    throw new Error(`${name} must contain at least ${minimum} item${minimum === 1 ? '' : 's'}`);
  }
  const values = value.map((item) => text(item, name));
  if (new Set(values).size !== values.length) throw new Error(`${name} must be unique`);
  return values;
}

function parseMeasurement(value: unknown, name = 'security measurement'): SecurityMeasurement {
  const body = record(value, name);
  exact(
    body,
    [
      'feature',
      'status',
      'scope',
      'value',
      'baseline',
      'unit',
      'window_start',
      'window_end',
      'window_count',
      'evidence_refs',
      'detail',
    ],
    name,
  );
  const feature = member(body.feature, FEATURE_SET, `${name} feature`);
  const status = member(
    body.status,
    new Set<SecurityMeasurement['status']>(['MEASURED', 'INSUFFICIENT']),
    `${name} status`,
  );
  const scope = nullableText(body.scope, `${name} scope`);
  const detail = text(body.detail, `${name} detail`);
  const windowCount = integer(body.window_count, `${name} window count`);
  const refs = uniqueTexts(body.evidence_refs, `${name} evidence refs`);

  if (status === 'INSUFFICIENT') {
    if (
      body.value !== null ||
      body.baseline !== null ||
      body.unit !== null ||
      body.window_start !== null ||
      body.window_end !== null ||
      windowCount !== 0 ||
      refs.length !== 0
    ) {
      throw new Error(`${name} insufficient status contradicts its numeric or evidence fields`);
    }
    return {
      feature,
      status,
      scope,
      value: null,
      baseline: null,
      unit: null,
      window_start: null,
      window_end: null,
      window_count: 0,
      evidence_refs: [],
      detail,
    };
  }

  if (scope === null || windowCount < 1 || refs.length === 0) {
    throw new Error(`${name} measured status requires scope, windows, and evidence refs`);
  }
  const measured = finite(body.value, `${name} value`);
  const baseline = finite(body.baseline, `${name} baseline`);
  const unit = member(body.unit, UNITS, `${name} unit`);
  const start = time(body.window_start, `${name} window start`);
  const end = time(body.window_end, `${name} window end`);
  if (Date.parse(end) < Date.parse(start)) throw new Error(`${name} window runs backwards`);
  if (!FEATURE_UNITS[feature].has(unit)) throw new Error(`${name} unit does not match its feature`);
  if (PROBABILITY_UNITS.has(unit)) {
    probability(measured, `${name} value`);
    probability(baseline, `${name} baseline`);
  } else if (measured < 0 || baseline < 0) {
    throw new Error(`${name} entropy and variation values cannot be negative`);
  }
  return {
    feature,
    status,
    scope,
    value: measured,
    baseline,
    unit,
    window_start: start,
    window_end: end,
    window_count: windowCount,
    evidence_refs: refs,
    detail,
  };
}

function parseFrame(value: unknown): DecompFrame {
  const body = record(value, 'security decomposition frame');
  exact(
    body,
    [
      'frame_id',
      'observation_id',
      'ts',
      'service',
      'signal',
      'observed',
      'explained_base',
      'explained_event',
      'residual',
      'band_low',
      'band_high',
      'residual_score',
      'context_ids',
    ],
    'security decomposition frame',
  );
  const observed = finite(body.observed, 'security decomposition observed');
  const base = finite(body.explained_base, 'security decomposition base');
  const event = finite(body.explained_event, 'security decomposition event');
  const residual = finite(body.residual, 'security decomposition residual');
  if (Math.abs(observed - (base + event + residual)) > 1e-8) {
    throw new Error('security decomposition identity failed');
  }
  const low = finite(body.band_low, 'security decomposition band low');
  const high = finite(body.band_high, 'security decomposition band high');
  if (low > high) throw new Error('security decomposition band runs backwards');
  return {
    frame_id: text(body.frame_id, 'security decomposition frame id'),
    observation_id: text(body.observation_id, 'security decomposition observation id'),
    ts: time(body.ts, 'security decomposition frame time'),
    service: text(body.service, 'security decomposition frame service'),
    signal: text(body.signal, 'security decomposition frame signal'),
    observed,
    explained_base: base,
    explained_event: event,
    residual,
    band_low: low,
    band_high: high,
    residual_score: probability(body.residual_score, 'security decomposition residual score'),
    context_ids: uniqueTexts(body.context_ids, 'security decomposition context ids'),
  };
}

function parseDecomposition(value: unknown): IncidentDecomposition {
  const body = record(value, 'security decomposition');
  exact(
    body,
    ['status', 'service', 'signal', 'start', 'end', 'frames', 'truncated', 'detail'],
    'security decomposition',
  );
  const status = member(
    body.status,
    new Set<IncidentDecomposition['status']>(['available', 'insufficient']),
    'security decomposition status',
  );
  const service = nullableText(body.service, 'security decomposition service');
  const signal = nullableText(body.signal, 'security decomposition signal');
  const start = time(body.start, 'security decomposition start');
  const end = time(body.end, 'security decomposition end');
  if (Date.parse(end) < Date.parse(start)) throw new Error('security decomposition window runs backwards');
  if (!Array.isArray(body.frames) || body.frames.length > 500) {
    throw new Error('security decomposition frames must be a bounded array');
  }
  const frames = body.frames.map(parseFrame);
  let previous = '';
  for (const frame of frames) {
    const identity = `${frame.ts}\u0000${frame.frame_id}`;
    if (
      identity <= previous ||
      frame.service !== service ||
      frame.signal !== signal ||
      Date.parse(frame.ts) < Date.parse(start) ||
      Date.parse(frame.ts) > Date.parse(end)
    ) {
      throw new Error('security decomposition frames are outside, mismatched, or out of order');
    }
    previous = identity;
  }
  if (
    (status === 'available' && (service === null || signal === null || frames.length === 0)) ||
    (status === 'insufficient' && (service !== null || signal !== null || frames.length !== 0))
  ) {
    throw new Error('security decomposition status contradicts its frames');
  }
  return {
    status,
    service,
    signal,
    start,
    end,
    frames,
    truncated: bool(body.truncated, 'security decomposition truncation'),
    detail: text(body.detail, 'security decomposition detail'),
  };
}

function parseTimelineEvent(value: unknown): SecurityTimelineEvent {
  const body = record(value, 'security timeline event');
  exact(
    body,
    [
      'episode_id',
      'kind',
      'service',
      'signal',
      'status',
      'opened_at',
      'last_breach_at',
      'closed_at',
      'peak_deformation_score',
      'breach_window_count',
      'evidence_refs',
      'detail',
    ],
    'security timeline event',
  );
  const opened = time(body.opened_at, 'security timeline opened at');
  const lastBreach = time(body.last_breach_at, 'security timeline last breach at');
  const closed = nullableTime(body.closed_at, 'security timeline closed at');
  if (Date.parse(lastBreach) < Date.parse(opened)) {
    throw new Error('security timeline breach predates opening');
  }
  if (closed !== null && Date.parse(closed) < Date.parse(lastBreach)) {
    throw new Error('security timeline closure predates its last breach');
  }
  return {
    episode_id: text(body.episode_id, 'security timeline episode id'),
    kind: member(body.kind, TIMELINE_KINDS, 'security timeline kind'),
    service: text(body.service, 'security timeline service'),
    signal: text(body.signal, 'security timeline signal'),
    status: member(body.status, EPISODE_STATES, 'security timeline status'),
    opened_at: opened,
    last_breach_at: lastBreach,
    closed_at: closed,
    peak_deformation_score: probability(
      body.peak_deformation_score,
      'security timeline peak deformation score',
    ),
    breach_window_count: integer(body.breach_window_count, 'security breach window count', 1),
    evidence_refs: uniqueTexts(body.evidence_refs, 'security timeline evidence refs', 1) as [
      string,
      ...string[],
    ],
    detail: text(body.detail, 'security timeline detail'),
  };
}

function parseCohort(value: unknown): SecurityCohort {
  const body = record(value, 'security cohort');
  exact(
    body,
    [
      'cohort_id',
      'request_count',
      'window_start',
      'window_end',
      'measurements',
      'evidence_refs',
      'detail',
    ],
    'security cohort',
  );
  const id = text(body.cohort_id, 'security cohort id');
  const start = time(body.window_start, 'security cohort window start');
  const end = time(body.window_end, 'security cohort window end');
  if (Date.parse(end) < Date.parse(start)) throw new Error('security cohort window runs backwards');
  if (!Array.isArray(body.measurements) || body.measurements.length !== COHORT_FEATURES.length) {
    throw new Error('security cohort must carry every canonical cohort feature');
  }
  const measurements = body.measurements.map((item, index) => {
    const measurement = parseMeasurement(item, 'security cohort measurement');
    if (measurement.feature !== COHORT_FEATURES[index] || measurement.scope !== id) {
      throw new Error('security cohort measurement order or evidence-owned scope is invalid');
    }
    if (
      measurement.status === 'MEASURED' &&
      (Date.parse(measurement.window_start!) < Date.parse(start) ||
        Date.parse(measurement.window_end!) > Date.parse(end))
    ) {
      throw new Error('security cohort measurement escapes its evidence window');
    }
    return measurement;
  }) as SecurityCohort['measurements'];
  return {
    cohort_id: id,
    request_count: integer(body.request_count, 'security cohort request count', 1),
    window_start: start,
    window_end: end,
    measurements,
    evidence_refs: uniqueTexts(body.evidence_refs, 'security cohort evidence refs', 1) as [
      string,
      ...string[],
    ],
    detail: text(body.detail, 'security cohort detail'),
  };
}

function parseMitigation(value: unknown): SecurityMitigation {
  const body = record(value, 'security mitigation');
  exact(
    body,
    [
      'state',
      'plan_revision',
      'rung_id',
      'action_kind',
      'target_ref',
      'estimated_blast_fraction',
      'updated_at',
      'protected_cohort_integrity',
      'detail',
    ],
    'security mitigation',
  );
  const state =
    body.state === null ? null : member(body.state, ACTION_STATES, 'security mitigation state');
  const planRevision =
    body.plan_revision === null
      ? null
      : integer(body.plan_revision, 'security mitigation plan revision', 1);
  const rung = nullableText(body.rung_id, 'security mitigation rung');
  const kind =
    body.action_kind === null
      ? null
      : member(body.action_kind, ACTION_KINDS, 'security mitigation action kind');
  const target = nullableText(body.target_ref, 'security mitigation target');
  const blast =
    body.estimated_blast_fraction === null
      ? null
      : probability(body.estimated_blast_fraction, 'security mitigation blast fraction');
  const updated = nullableTime(body.updated_at, 'security mitigation updated at');
  const attached = [planRevision, rung, kind, target, blast, updated].every((item) => item !== null);
  if ((state !== null) !== attached) {
    throw new Error('security mitigation safety fields must travel together');
  }
  const integrity = parseMeasurement(
    body.protected_cohort_integrity,
    'protected cohort integrity',
  );
  if (integrity.feature !== 'PROTECTED_COHORT_INTEGRITY') {
    throw new Error('security mitigation integrity uses the wrong feature');
  }
  return {
    state,
    plan_revision: planRevision,
    rung_id: rung,
    action_kind: kind,
    target_ref: target,
    estimated_blast_fraction: blast,
    updated_at: updated,
    protected_cohort_integrity: integrity,
    detail: text(body.detail, 'security mitigation detail'),
  };
}

function parseSnapshot(value: unknown): SecuritySnapshot {
  const body = record(value, 'security snapshot');
  exact(
    body,
    [
      'incident_id',
      'opened_at',
      'updated_at',
      'state',
      'honesty',
      'timeline',
      'measurements',
      'suspect_cohorts',
      'decomposition',
      'mitigation',
    ],
    'security snapshot',
  );
  const opened = time(body.opened_at, 'security snapshot opened at');
  const updated = time(body.updated_at, 'security snapshot updated at');
  if (Date.parse(updated) < Date.parse(opened)) throw new Error('security snapshot predates its incident');
  if (!Array.isArray(body.measurements) || body.measurements.length !== FEATURES.length) {
    throw new Error('security snapshot must carry every canonical security feature');
  }
  const measurements = body.measurements.map((item, index) => {
    const measurement = parseMeasurement(item);
    if (measurement.feature !== FEATURES[index]) {
      throw new Error('security snapshot measurements are outside canonical order');
    }
    return measurement;
  }) as SecuritySnapshot['measurements'];
  if (!Array.isArray(body.timeline)) throw new Error('security timeline must be an array');
  const timeline = body.timeline.map(parseTimelineEvent);
  let previousTimeline = '';
  const episodeIds = new Set<string>();
  for (const event of timeline) {
    const identity = `${event.opened_at}\u0000${event.episode_id}`;
    if (identity < previousTimeline || episodeIds.has(event.episode_id)) {
      throw new Error('security timeline is out of order or repeats an episode');
    }
    previousTimeline = identity;
    episodeIds.add(event.episode_id);
  }
  if (!Array.isArray(body.suspect_cohorts)) throw new Error('security cohorts must be an array');
  const cohorts = body.suspect_cohorts.map(parseCohort);
  const ids = cohorts.map((cohort) => cohort.cohort_id);
  if (new Set(ids).size !== ids.length || ids.some((id, index) => index > 0 && id < ids[index - 1]!)) {
    throw new Error('security cohorts must be unique and ordered by evidence-owned id');
  }
  return {
    incident_id: text(body.incident_id, 'security incident id'),
    opened_at: opened,
    updated_at: updated,
    state: member(body.state, INCIDENT_STATES, 'security incident state'),
    honesty: member(body.honesty, new Set(['REAL', 'SIMULATED']), 'security honesty'),
    timeline,
    measurements,
    suspect_cohorts: cohorts,
    decomposition: parseDecomposition(body.decomposition),
    mitigation: parseMitigation(body.mitigation),
  };
}

export function parseSecurityResponse(value: unknown): SecurityResponse {
  const body = record(value, 'security response');
  exact(body, ['status', 'snapshot', 'detail'], 'security response');
  const status = member(
    body.status,
    new Set<SecurityResponse['status']>(['ready', 'empty', 'degraded']),
    'security response status',
  );
  const detail = nullableText(body.detail, 'security response detail');
  if (status === 'ready') {
    if (body.snapshot === null || detail !== null) {
      throw new Error('ready security response must carry only a snapshot');
    }
    return { status, snapshot: parseSnapshot(body.snapshot), detail: null };
  }
  if (body.snapshot !== null || detail === null) {
    throw new Error('unavailable security response must carry only detail');
  }
  return { status, snapshot: null, detail };
}

export async function fetchSecurity(
  credential: string | null,
  signal?: AbortSignal,
): Promise<SecurityResponse> {
  const init: RequestInit = {};
  if (credential !== null) init.headers = { 'x-sentinel-secret': credential };
  if (signal !== undefined) init.signal = signal;
  const response = await fetch('/api/security', init);
  if (response.status === 401) throw new SecurityCredentialRequiredError();
  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`Security snapshot request failed (${response.status})`);
  }
  return parseSecurityResponse(await response.json());
}
