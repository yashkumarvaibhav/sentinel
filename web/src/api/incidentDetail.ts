import { parseCausalGraph } from '@/api/causalGraph';
import { CredentialRequiredError } from '@/api/actionControl';
import type {
  AuditEventKind,
  CheckOutcome,
  DecisionAction,
  EvidenceAxis,
  EvidenceDirection,
  IncidentDetail,
  IncidentDetailResponse,
  IncidentSeverity,
  IncidentState,
  ReasonSubtype,
  SymptomKind,
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
const DIRECTIONS = new Set<EvidenceDirection>([
  'ABOVE_BASELINE',
  'BELOW_BASELINE',
  'AT_BASELINE',
]);
const AXES = new Set<EvidenceAxis>([
  'SECURITY',
  'RELIABILITY',
  'CHANGE_CONFIG',
  'BUSINESS_IMPACT',
]);
const KINDS = new Set<SymptomKind>([
  'RESIDUAL_EXCEED',
  'RATIO_DEFORM',
  'LOG_BURST',
  'EDGE_DEGRADED',
  'SATURATION',
  'DEPLOY_MARKER',
  'DROP',
  'SILENCE',
]);
const CHECKS = new Set(['temporal_causality', 'trace_coverage', 'dependency_validity', 'memory_similarity']);
const CHECK_OUTCOMES = new Set<CheckOutcome>(['PASSED', 'FAILED', 'BOOTSTRAP']);
const DECISIONS = new Set<DecisionAction>([
  'SUPPRESS',
  'ALERT',
  'ACT',
  'ESCALATE_TO_HUMAN',
  'AUTO_CONTAIN_THEN_ESCALATE',
]);
const AUDIT_KINDS = new Set<AuditEventKind>([
  'DECISION',
  'ACTION_PLANNED',
  'ACTION_APPLIED',
  'ACTION_VERIFIED',
  'ACTION_REVERTED',
  'ACTION_REFUSED',
  'ROLLBACK',
  'BREAKER_OPENED',
  'APPROVAL',
]);

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exact(body: Record<string, unknown>, expected: readonly string[], name: string): void {
  const allowed = new Set(expected);
  const unknown = Object.keys(body).filter((key) => !allowed.has(key));
  const missing = expected.filter((key) => !(key in body));
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

function finite(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${name} must be finite`);
  }
  return value;
}

function probability(value: unknown, name: string): number {
  const measured = finite(value, name);
  if (measured < 0 || measured > 1) throw new Error(`${name} must be within [0, 1]`);
  return measured;
}

function integer(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return value;
}

function boolean(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${name} must be boolean`);
  return value;
}

function member<T extends string>(value: unknown, values: Set<T>, name: string): T {
  if (typeof value !== 'string' || !values.has(value as T)) throw new Error(`${name} is unknown`);
  return value as T;
}

function uniqueTexts(value: unknown, name: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${name} must be an array`);
  const values = value.map((item) => text(item, name));
  if (new Set(values).size !== values.length) throw new Error(`${name} must be unique`);
  return values;
}

function parseConfidence(value: unknown): void {
  const body = record(value, 'incident calibration');
  exact(body, ['status', 'value', 'note'], 'incident calibration');
  if (body.status !== 'calibrated' && body.status !== 'insufficient') {
    throw new Error('incident calibration status is unknown');
  }
  const measured = body.value === null ? null : probability(body.value, 'incident calibration value');
  if ((body.status === 'calibrated') !== (measured !== null)) {
    throw new Error('incident calibration value contradicts its status');
  }
  text(body.note, 'incident calibration note');
}

function parseVerdict(value: unknown): void {
  const body = record(value, 'incident verdict proof');
  exact(
    body,
    ['status', 'verdict_class', 'reason_subtype', 'distribution', 'calibration'],
    'incident verdict proof',
  );
  if (body.status !== 'decided' && body.status !== 'insufficient') {
    throw new Error('incident verdict status is unknown');
  }
  const verdict = body.verdict_class === null ? null : member(body.verdict_class, VERDICTS, 'incident verdict');
  if (body.reason_subtype !== null && body.reason_subtype !== ('CAPACITY_SHORTAGE' satisfies ReasonSubtype)) {
    throw new Error('incident reason subtype is unknown');
  }
  if (!Array.isArray(body.distribution)) throw new Error('incident distribution must be an array');
  const classes = body.distribution.map((point) => {
    const item = record(point, 'incident distribution point');
    exact(item, ['verdict_class', 'probability'], 'incident distribution point');
    probability(item.probability, 'incident distribution probability');
    return member(item.verdict_class, VERDICTS, 'incident distribution class');
  });
  if (
    (body.status === 'insufficient' && (verdict !== null || classes.length !== 0)) ||
    (body.status === 'decided' &&
      (verdict === null || classes.length !== VERDICTS.size || new Set(classes).size !== VERDICTS.size))
  ) {
    throw new Error('incident verdict status contradicts its distribution');
  }
  parseConfidence(body.calibration);
}

function parseDecomposition(value: unknown): void {
  const body = record(value, 'incident decomposition');
  exact(
    body,
    ['status', 'service', 'signal', 'start', 'end', 'frames', 'truncated', 'detail'],
    'incident decomposition',
  );
  if (body.status !== 'available' && body.status !== 'insufficient') {
    throw new Error('incident decomposition status is unknown');
  }
  const service = nullableText(body.service, 'incident decomposition service');
  const signal = nullableText(body.signal, 'incident decomposition signal');
  const start = time(body.start, 'incident decomposition start');
  const end = time(body.end, 'incident decomposition end');
  if (Date.parse(end) < Date.parse(start)) throw new Error('incident decomposition window runs backwards');
  if (!Array.isArray(body.frames) || body.frames.length > 500) {
    throw new Error('incident decomposition frames must be a bounded array');
  }
  let previous = '';
  for (const frameValue of body.frames) {
    const frame = record(frameValue, 'incident decomposition frame');
    exact(
      frame,
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
      'incident decomposition frame',
    );
    const frameId = text(frame.frame_id, 'decomposition frame id');
    text(frame.observation_id, 'decomposition observation id');
    const frameTime = time(frame.ts, 'decomposition frame time');
    if (frame.service !== service || frame.signal !== signal) {
      throw new Error('decomposition frame does not belong to its named series');
    }
    const observed = finite(frame.observed, 'decomposition observed');
    const base = finite(frame.explained_base, 'decomposition base');
    const event = finite(frame.explained_event, 'decomposition event');
    const residual = finite(frame.residual, 'decomposition residual');
    if (Math.abs(observed - (base + event + residual)) > 1e-8) {
      throw new Error('incident decomposition identity failed');
    }
    const low = finite(frame.band_low, 'decomposition band low');
    const high = finite(frame.band_high, 'decomposition band high');
    if (low > high) throw new Error('incident decomposition band runs backwards');
    probability(frame.residual_score, 'decomposition residual score');
    uniqueTexts(frame.context_ids, 'decomposition context ids');
    const identity = `${frameTime}\u0000${frameId}`;
    if (identity <= previous || Date.parse(frameTime) < Date.parse(start) || Date.parse(frameTime) > Date.parse(end)) {
      throw new Error('incident decomposition frames are outside or out of order');
    }
    previous = identity;
  }
  boolean(body.truncated, 'incident decomposition truncation');
  text(body.detail, 'incident decomposition detail');
  if (
    (body.status === 'available' && (service === null || signal === null || body.frames.length === 0)) ||
    (body.status === 'insufficient' && (service !== null || signal !== null || body.frames.length !== 0))
  ) {
    throw new Error('incident decomposition status contradicts its frames');
  }
}

function parseEvidence(value: unknown): void {
  const body = record(value, 'incident evidence proof');
  exact(
    body,
    [
      'assessment_id',
      'axis',
      'symptom_kinds',
      'services',
      'feature',
      'value',
      'baseline',
      'direction',
      'contribution',
      'note',
      'evidence_refs',
    ],
    'incident evidence proof',
  );
  text(body.assessment_id, 'incident assessment id');
  member(body.axis, AXES, 'incident evidence axis');
  if (!Array.isArray(body.symptom_kinds)) throw new Error('incident symptom kinds must be an array');
  const kinds = body.symptom_kinds.map((kind) => member(kind, KINDS, 'incident symptom kind'));
  if (new Set(kinds).size !== kinds.length) throw new Error('incident symptom kinds must be unique');
  uniqueTexts(body.services, 'incident evidence services');
  text(body.feature, 'incident evidence feature');
  const measured = finite(body.value, 'incident evidence value');
  const baseline = finite(body.baseline, 'incident evidence baseline');
  const direction = member(body.direction, DIRECTIONS, 'incident evidence direction');
  if (
    (measured > baseline && direction !== 'ABOVE_BASELINE') ||
    (measured < baseline && direction !== 'BELOW_BASELINE') ||
    (measured === baseline && direction !== 'AT_BASELINE')
  ) {
    throw new Error('incident evidence direction contradicts its values');
  }
  probability(body.contribution, 'incident evidence contribution');
  text(body.note, 'incident evidence note');
  uniqueTexts(body.evidence_refs, 'incident evidence refs');
}

function parseVerification(value: unknown, incidentId: string): void {
  const body = record(value, 'incident verification');
  exact(body, ['verification_id', 'ts', 'incident_id', 'confirmed', 'checks'], 'incident verification');
  text(body.verification_id, 'verification id');
  time(body.ts, 'verification time');
  if (body.incident_id !== incidentId) throw new Error('verification belongs to another incident');
  const confirmed = boolean(body.confirmed, 'verification confirmation');
  if (!Array.isArray(body.checks)) throw new Error('verification checks must be an array');
  const names = body.checks.map((checkValue) => {
    const check = record(checkValue, 'verification check');
    exact(check, ['name', 'outcome', 'detail'], 'verification check');
    const name = text(check.name, 'verification check name');
    if (!CHECKS.has(name)) throw new Error('verification check is unknown');
    member(check.outcome, CHECK_OUTCOMES, 'verification check outcome');
    text(check.detail, 'verification check detail');
    return { name, outcome: check.outcome };
  });
  if (names.length !== CHECKS.size || new Set(names.map(({ name }) => name)).size !== CHECKS.size) {
    throw new Error('verification must contain the fixed four checks');
  }
  if (confirmed !== names.every(({ outcome }) => outcome !== 'FAILED')) {
    throw new Error('verification confirmation contradicts its checks');
  }
}

function parseDecision(value: unknown, incidentId: string): void {
  const body = record(value, 'incident decision');
  exact(
    body,
    [
      'decision_id',
      'ts',
      'incident_id',
      'action',
      'rule_id',
      'reason',
      'evidence_ts',
      'severity',
      'confirmed',
      'verification_id',
      'requires_human_approval',
      'verdict_class',
      'verdict_id',
      'confidence',
      'target_service',
      'approval_reasons',
      'escalation_reasons',
      'guards_applied',
      'floors_applied',
      'suppression',
    ],
    'incident decision',
  );
  text(body.decision_id, 'decision id');
  time(body.ts, 'decision time');
  if (body.incident_id !== incidentId) throw new Error('decision belongs to another incident');
  member(body.action, DECISIONS, 'decision action');
  text(body.rule_id, 'decision rule');
  text(body.reason, 'decision reason');
  time(body.evidence_ts, 'decision evidence time');
  member(body.severity, SEVERITIES, 'decision severity');
  boolean(body.confirmed, 'decision confirmation');
  text(body.verification_id, 'decision verification id');
  const approval = boolean(body.requires_human_approval, 'decision approval state');
  const approvalReasons = uniqueTexts(body.approval_reasons, 'decision approval reasons');
  if (approval !== (approvalReasons.length > 0)) throw new Error('decision approval contradicts its reasons');
  uniqueTexts(body.escalation_reasons, 'decision escalation reasons');
  uniqueTexts(body.guards_applied, 'decision guards');
  uniqueTexts(body.floors_applied, 'decision floors');
  nullableText(body.verdict_id, 'decision verdict id');
  if (body.verdict_class !== null) member(body.verdict_class, VERDICTS, 'decision verdict class');
  if (body.confidence !== null) probability(body.confidence, 'decision confidence');
  nullableText(body.target_service, 'decision target');
  if (body.suppression !== null) {
    const suppression = record(body.suppression, 'decision suppression');
    exact(suppression, ['window_id', 'kind', 'owner', 'reason', 'expires_ts'], 'decision suppression');
    text(suppression.window_id, 'suppression window');
    if (suppression.kind !== 'CHANGE_FREEZE' && suppression.kind !== 'MAINTENANCE') {
      throw new Error('suppression kind is unknown');
    }
    text(suppression.owner, 'suppression owner');
    text(suppression.reason, 'suppression reason');
    time(suppression.expires_ts, 'suppression expiry');
  }
}

function parseAuditEntry(value: unknown, incidentId: string): number {
  const body = record(value, 'incident action record');
  exact(
    body,
    [
      'entry_id',
      'sequence',
      'ts',
      'kind',
      'actor',
      'summary',
      'incident_id',
      'decision_id',
      'plan_id',
      'body',
      'previous_hash',
      'entry_hash',
      'honesty',
    ],
    'incident action record',
  );
  text(body.entry_id, 'action record id');
  const sequence = integer(body.sequence, 'action record sequence');
  time(body.ts, 'action record time');
  member(body.kind, AUDIT_KINDS, 'action record kind');
  text(body.actor, 'action record actor');
  text(body.summary, 'action record summary');
  if (body.incident_id !== incidentId) throw new Error('action record belongs to another incident');
  nullableText(body.decision_id, 'action record decision');
  nullableText(body.plan_id, 'action record plan');
  record(body.body, 'action record body');
  for (const key of ['previous_hash', 'entry_hash'] as const) {
    if (typeof body[key] !== 'string' || !/^[0-9a-f]{64}$/.test(body[key])) {
      throw new Error(`action record ${key} must be a sha256 digest`);
    }
  }
  if (body.honesty !== 'REAL' && body.honesty !== 'SIMULATED') {
    throw new Error('action record honesty is unknown');
  }
  return sequence;
}

function parseDetail(value: unknown): IncidentDetail {
  const body = record(value, 'incident detail');
  exact(
    body,
    [
      'incident_id',
      'opened_at',
      'updated_at',
      'state',
      'severity',
      'services',
      'origin_service',
      'origin_confidence',
      'verdict',
      'reason',
      'rejected_alternatives',
      'decomposition',
      'evidence',
      'causal_graph',
      'verification',
      'action_log',
      'provenance',
    ],
    'incident detail',
  );
  const incidentId = text(body.incident_id, 'incident detail id');
  time(body.opened_at, 'incident opening');
  time(body.updated_at, 'incident update');
  member(body.state, STATES, 'incident state');
  member(body.severity, SEVERITIES, 'incident severity');
  const services = uniqueTexts(body.services, 'incident services');
  if (services.length === 0) throw new Error('incident services must not be empty');
  const origin = nullableText(body.origin_service, 'incident origin');
  if (origin !== null && !services.includes(origin)) throw new Error('incident origin must be a named service');
  if (body.origin_confidence !== null) probability(body.origin_confidence, 'incident origin confidence');
  if ((origin === null) !== (body.origin_confidence === null)) {
    throw new Error('incident origin and confidence must appear together');
  }
  parseVerdict(body.verdict);
  text(body.reason, 'incident reason');
  if (!Array.isArray(body.rejected_alternatives)) throw new Error('rejected alternatives must be an array');
  const alternatives = body.rejected_alternatives.map((value) => {
    const alternative = record(value, 'rejected alternative');
    exact(alternative, ['verdict_class', 'reason'], 'rejected alternative');
    text(alternative.reason, 'rejected alternative reason');
    return member(alternative.verdict_class, VERDICTS, 'rejected alternative class');
  });
  if (new Set(alternatives).size !== alternatives.length) {
    throw new Error('rejected alternatives must be unique');
  }
  parseDecomposition(body.decomposition);
  if (!Array.isArray(body.evidence)) throw new Error('incident evidence must be an array');
  body.evidence.forEach(parseEvidence);
  const graph = parseCausalGraph(body.causal_graph, { allowResolved: true });
  if (graph.incident_id !== incidentId) throw new Error('causal graph belongs to another incident');
  parseVerification(body.verification, incidentId);
  const actionLog = record(body.action_log, 'incident action log');
  exact(actionLog, ['decision', 'entries', 'detail'], 'incident action log');
  parseDecision(actionLog.decision, incidentId);
  if (!Array.isArray(actionLog.entries)) throw new Error('incident action records must be an array');
  const sequences = actionLog.entries.map((entry) => parseAuditEntry(entry, incidentId));
  if (sequences.some((sequence, index) => index > 0 && sequence <= sequences[index - 1]!)) {
    throw new Error('incident action records must be uniquely ordered');
  }
  text(actionLog.detail, 'incident action log detail');
  const provenance = record(body.provenance, 'incident provenance');
  exact(provenance, ['telemetry', 'stimulus', 'mode', 'capture_id', 'seed'], 'incident provenance');
  if (provenance.telemetry !== 'REAL' && provenance.telemetry !== 'SIMULATED') {
    throw new Error('telemetry provenance is unknown');
  }
  if (provenance.stimulus !== 'REAL' && provenance.stimulus !== 'SIMULATED') {
    throw new Error('stimulus provenance is unknown');
  }
  if (provenance.mode !== 'LIVE' && provenance.mode !== 'REPLAY') {
    throw new Error('incident proof mode is unknown');
  }
  const capture = nullableText(provenance.capture_id, 'incident capture id');
  if (provenance.seed !== null) integer(provenance.seed, 'incident seed');
  if (provenance.mode === 'REPLAY' && capture === null) throw new Error('a replay must name its capture');
  return body as unknown as IncidentDetail;
}

export function parseIncidentDetailResponse(value: unknown): IncidentDetailResponse {
  const body = record(value, 'incident detail response');
  exact(body, ['status', 'detail', 'message'], 'incident detail response');
  if (body.status !== 'ready' && body.status !== 'not_found' && body.status !== 'degraded') {
    throw new Error('incident detail response status is unknown');
  }
  const detail = body.detail === null ? null : parseDetail(body.detail);
  const message = nullableText(body.message, 'incident detail response message');
  if (
    (body.status === 'ready' && (detail === null || message !== null)) ||
    (body.status !== 'ready' && (detail !== null || message === null))
  ) {
    throw new Error('incident detail response status contradicts its payload');
  }
  return { status: body.status, detail, message };
}

export async function fetchIncidentDetail(
  incidentId: string,
  credential: string | null = null,
  signal?: AbortSignal,
): Promise<IncidentDetailResponse> {
  const response = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}`, {
    ...(credential === null ? {} : { headers: { 'x-sentinel-secret': credential } }),
    ...(signal ? { signal } : {}),
  });
  if (response.status === 401) throw new CredentialRequiredError();
  if (response.status !== 200 && response.status !== 404 && response.status !== 503) {
    throw new Error(`incident detail request failed: ${response.status}`);
  }
  return parseIncidentDetailResponse(await response.json());
}
