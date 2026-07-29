import type {
  ActionControlIntent,
  ActionControlResponse,
  ActionControlSnapshot,
  ActionKind,
  ActionStatus,
  ActuatorKind,
} from '@/contracts/types';

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
const ACTUATORS = new Set<ActuatorKind>(['KUBERNETES', 'MESH', 'FEATURE_FLAG', 'SIMULATED']);
const CONTROL_STATES = new Set([
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
const OUTCOME_STATUSES = new Set<ActionStatus>([
  'SIMULATED',
  'APPLIED',
  'VERIFIED',
  'REVERTED',
  'FAILED',
]);

export class CredentialRequiredError extends Error {
  constructor() {
    super('An operator credential is required for this protected incident action.');
    this.name = 'CredentialRequiredError';
  }
}

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
  if (unknown.length > 0)
    throw new Error(`${name} has unknown field: ${unknown.sort().join(', ')}`);
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

function boolean(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${name} must be boolean`);
  return value;
}

function integer(value: unknown, name: string, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum) {
    throw new Error(`${name} must be an integer greater than or equal to ${minimum}`);
  }
  return value;
}

function probability(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 1) {
    throw new Error(`${name} must be finite within [0, 1]`);
  }
  return value;
}

function nonnegative(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be finite and non-negative`);
  }
  return value;
}

function member<T extends string>(value: unknown, values: Set<T>, name: string): T {
  if (typeof value !== 'string' || !values.has(value as T)) throw new Error(`${name} is unknown`);
  return value as T;
}

function texts(value: unknown, name: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${name} must be an array`);
  const parsed = value.map((item) => text(item, name));
  if (new Set(parsed).size !== parsed.length) throw new Error(`${name} must be unique`);
  return parsed;
}

function parameters(value: unknown, name: string): Record<string, string | boolean | number> {
  const body = record(value, name);
  for (const [key, item] of Object.entries(body)) {
    text(key, `${name} key`);
    if (
      typeof item !== 'string' &&
      typeof item !== 'boolean' &&
      (typeof item !== 'number' || !Number.isFinite(item))
    ) {
      throw new Error(`${name}.${key} must be a finite scalar`);
    }
  }
  return body as Record<string, string | boolean | number>;
}

function sameParameters(
  left: Record<string, string | boolean | number>,
  right: Record<string, string | boolean | number>,
): boolean {
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return (
    JSON.stringify(leftKeys) === JSON.stringify(rightKeys) &&
    leftKeys.every((key) => left[key] === right[key])
  );
}

function parsePlan(
  value: unknown,
  incidentId: string,
): {
  body: Record<string, unknown>;
  actuator: ActuatorKind;
  actionKind: ActionKind;
  parameters: Record<string, string | boolean | number>;
} {
  const body = record(value, 'action plan');
  exact(
    body,
    [
      'plan_id',
      'ts',
      'decision_id',
      'incident_id',
      'actuator',
      'action_kind',
      'target_service',
      'target_ref',
      'parameters',
      'reason',
      'expected_effect',
      'reversible',
      'requires_human_approval',
      'estimated_blast_fraction',
      'idempotency_key',
      'honesty',
    ],
    'action plan',
  );
  text(body.plan_id, 'plan id');
  time(body.ts, 'plan time');
  text(body.decision_id, 'plan decision id');
  if (body.incident_id !== incidentId) throw new Error('action plan belongs to another incident');
  const actuator = member(body.actuator, ACTUATORS, 'plan actuator');
  const actionKind = member(body.action_kind, ACTION_KINDS, 'plan action kind');
  text(body.target_service, 'plan target service');
  text(body.target_ref, 'plan target reference');
  const parsedParameters = parameters(body.parameters, 'plan parameters');
  text(body.reason, 'plan reason');
  text(body.expected_effect, 'plan expected effect');
  boolean(body.reversible, 'plan reversible');
  boolean(body.requires_human_approval, 'plan approval requirement');
  probability(body.estimated_blast_fraction, 'plan blast radius');
  text(body.idempotency_key, 'plan idempotency key');
  if (body.honesty !== 'REAL' && body.honesty !== 'SIMULATED') {
    throw new Error('plan honesty is unknown');
  }
  return { body, actuator, actionKind, parameters: parsedParameters };
}

function parseRung(value: unknown, plan: ReturnType<typeof parsePlan>): Record<string, unknown> {
  const body = record(value, 'action rung');
  exact(
    body,
    [
      'rung_id',
      'ladder_id',
      'actuator',
      'action_kind',
      'parameters',
      'ttl_seconds',
      'requires_human_approval',
      'required_approval_count',
      'maximum_blast_fraction',
      'reason',
      'canary_parameter',
      'canary_shares',
    ],
    'action rung',
  );
  text(body.rung_id, 'rung id');
  text(body.ladder_id, 'ladder id');
  const actuator = member(body.actuator, ACTUATORS, 'rung actuator');
  const actionKind = member(body.action_kind, ACTION_KINDS, 'rung action kind');
  const rungParameters = parameters(body.parameters, 'rung parameters');
  integer(body.ttl_seconds, 'rung TTL', 1);
  const approval = boolean(body.requires_human_approval, 'rung approval requirement');
  const approvalCount = integer(body.required_approval_count, 'rung approval count');
  if (approvalCount > 2) throw new Error('rung approval count exceeds the supported policy');
  const expectedApprovals =
    actionKind === 'ISOLATE' || actionKind === 'ROLLBACK' ? 2 : Number(approval);
  if (approvalCount !== expectedApprovals) {
    throw new Error('rung approval count disagrees with the destructive-action policy');
  }
  probability(body.maximum_blast_fraction, 'rung blast ceiling');
  text(body.reason, 'rung reason');
  const canaryParameter = nullableText(body.canary_parameter, 'rung canary parameter');
  const shares = Array.isArray(body.canary_shares)
    ? body.canary_shares.map((share) => integer(share, 'canary share', 1))
    : (() => {
        throw new Error('rung canary shares must be an array');
      })();
  if (
    shares.some((share) => share > 100) ||
    new Set(shares).size !== shares.length ||
    shares.some((share, index) => index > 0 && share <= shares[index - 1]!)
  ) {
    throw new Error('rung canary shares must be unique increasing percentages');
  }
  if ((canaryParameter === null) !== (shares.length === 0)) {
    throw new Error('rung canary parameter and shares must travel together');
  }
  if (
    actuator !== plan.actuator ||
    actionKind !== plan.actionKind ||
    !sameParameters(rungParameters, plan.parameters) ||
    approval !== plan.body.requires_human_approval
  ) {
    throw new Error('rung and plan safety-critical fields disagree');
  }
  return body;
}

function parseOutcome(
  value: unknown,
  plan: ReturnType<typeof parsePlan>,
): { body: Record<string, unknown>; status: ActionStatus } | null {
  if (value === null) return null;
  const body = record(value, 'action outcome');
  exact(
    body,
    [
      'outcome_id',
      'ts',
      'plan_id',
      'idempotency_key',
      'status',
      'dry_run',
      'detail',
      'deduplicated',
      'observed',
      'approvals',
      'gates_passed',
      'revert_token',
      'honesty',
    ],
    'action outcome',
  );
  text(body.outcome_id, 'outcome id');
  time(body.ts, 'outcome time');
  if (body.plan_id !== plan.body.plan_id || body.idempotency_key !== plan.body.idempotency_key) {
    throw new Error('action outcome does not belong to its server-held plan');
  }
  const status = member(body.status, OUTCOME_STATUSES, 'outcome status');
  const dryRun = boolean(body.dry_run, 'outcome dry run');
  if ((status === 'SIMULATED') !== dryRun)
    throw new Error('outcome dry-run status contradicts itself');
  text(body.detail, 'outcome detail');
  boolean(body.deduplicated, 'outcome deduplication');
  texts(body.observed, 'outcome observations');
  texts(body.approvals, 'outcome approvals');
  texts(body.gates_passed, 'outcome gates');
  nullableText(body.revert_token, 'outcome revert token');
  if (body.honesty !== 'REAL' && body.honesty !== 'SIMULATED') {
    throw new Error('outcome honesty is unknown');
  }
  return { body, status };
}

function parseSloSample(value: unknown): Record<string, unknown> {
  const body = record(value, 'action SLO sample');
  exact(
    body,
    [
      'service',
      'sampled_at',
      'status',
      'availability',
      'latency_p95_ms',
      'availability_target',
      'latency_p95_target_ms',
    ],
    'action SLO sample',
  );
  text(body.service, 'SLO service');
  time(body.sampled_at, 'SLO sample time');
  if (body.status !== 'MEASURED' && body.status !== 'INSUFFICIENT') {
    throw new Error('SLO sample status is unknown');
  }
  const measured = body.availability !== null && body.latency_p95_ms !== null;
  if (measured !== (body.status === 'MEASURED')) {
    throw new Error('SLO sample measurement contradicts its status');
  }
  if (measured) {
    probability(body.availability, 'SLO availability');
    nonnegative(body.latency_p95_ms, 'SLO latency');
  }
  probability(body.availability_target, 'SLO availability target');
  if (nonnegative(body.latency_p95_target_ms, 'SLO latency target') === 0) {
    throw new Error('SLO latency target must be positive');
  }
  return body;
}

function parseRollbackVerification(
  value: unknown,
  beforeSamples: Record<string, unknown>[],
): Record<string, unknown> | null {
  if (value === null) return null;
  const body = record(value, 'rollback verification');
  exact(
    body,
    ['status', 'verified_at', 'checked_signals', 'before', 'after', 'users_restored', 'detail'],
    'rollback verification',
  );
  if (!['VERIFIED', 'FAILED', 'INSUFFICIENT'].includes(String(body.status))) {
    throw new Error('rollback verification status is unknown');
  }
  time(body.verified_at, 'rollback verification time');
  if (
    !Array.isArray(body.checked_signals) ||
    JSON.stringify(body.checked_signals) !== JSON.stringify(['availability', 'latency_p95_ms'])
  ) {
    throw new Error('rollback verification must check availability then latency');
  }
  if (!Array.isArray(body.before) || !Array.isArray(body.after)) {
    throw new Error('rollback verification needs before and after SLO samples');
  }
  const before = body.before.map(parseSloSample);
  const after = body.after.map(parseSloSample);
  if (JSON.stringify(before) !== JSON.stringify(beforeSamples)) {
    throw new Error('rollback verification does not use the durable pre-revert samples');
  }
  if (
    before.length === 0 ||
    before.length !== after.length ||
    before.some((sample, index) => sample.service !== after[index]?.service)
  ) {
    throw new Error('rollback verification must compare the same protected services');
  }
  const insufficient = [...before, ...after].some((sample) => sample.status === 'INSUFFICIENT');
  if (insufficient !== (body.status === 'INSUFFICIENT')) {
    throw new Error('missing SLO telemetry must remain insufficient');
  }
  if (body.users_restored === null) {
    if (!insufficient) throw new Error('measured rollback proof must record users restored');
  } else {
    if (insufficient) throw new Error('insufficient rollback proof cannot claim users restored');
    probability(body.users_restored, 'users restored');
  }
  text(body.detail, 'rollback verification detail');
  return body;
}

function parseControl(value: unknown): ActionControlSnapshot {
  const body = record(value, 'action control');
  exact(
    body,
    [
      'incident_id',
      'plan_revision',
      'state',
      'rung',
      'plan',
      'guard_results',
      'latest_outcome',
      'rollback_slo_before',
      'rollback_verification',
      'approvals',
      'rejected_by',
      'rejected_at',
      'created_at',
      'updated_at',
    ],
    'action control',
  );
  const incidentId = text(body.incident_id, 'action-control incident');
  integer(body.plan_revision, 'plan revision', 1);
  const state = member(body.state, CONTROL_STATES, 'action-control state');
  const plan = parsePlan(body.plan, incidentId);
  const rung = parseRung(body.rung, plan);
  if (!Array.isArray(body.guard_results)) throw new Error('guard results must be an array');
  const gateIds = body.guard_results.map((value) => {
    const gate = record(value, 'guard result');
    exact(gate, ['gate_id', 'status', 'detail'], 'guard result');
    const gateId = text(gate.gate_id, 'guard id');
    if (gate.status !== 'PASSED' && gate.status !== 'REFUSED') {
      throw new Error('guard status is unknown');
    }
    text(gate.detail, 'guard detail');
    return gateId;
  });
  if (new Set(gateIds).size !== gateIds.length) throw new Error('guard results must be unique');
  const outcome = parseOutcome(body.latest_outcome, plan);
  if (!Array.isArray(body.rollback_slo_before)) {
    throw new Error('pre-revert SLO evidence must be an array');
  }
  const rollbackBefore = body.rollback_slo_before.map(parseSloSample);
  const rollbackVerification = parseRollbackVerification(
    body.rollback_verification,
    rollbackBefore,
  );
  if (
    state !== 'ROLLED_BACK' &&
    (rollbackBefore.length > 0 || rollbackVerification !== null)
  ) {
    throw new Error('only a rolled-back control may carry rollback SLO evidence');
  }
  if (!Array.isArray(body.approvals)) throw new Error('control approvals must be an array');
  const actors = body.approvals.map((value) => {
    const approval = record(value, 'control approval');
    exact(approval, ['actor', 'approved_at'], 'control approval');
    time(approval.approved_at, 'approval time');
    return text(approval.actor, 'approval actor');
  });
  if (new Set(actors).size !== actors.length) throw new Error('control approvals must be unique');
  const rejectedBy = nullableText(body.rejected_by, 'rejection actor');
  const rejectedAt = body.rejected_at === null ? null : time(body.rejected_at, 'rejection time');
  if ((state === 'REJECTED') !== (rejectedBy !== null && rejectedAt !== null)) {
    throw new Error('rejection state contradicts its server identity');
  }
  const created = time(body.created_at, 'control creation time');
  const updated = time(body.updated_at, 'control update time');
  if (Date.parse(updated) < Date.parse(created))
    throw new Error('control update predates creation');
  const expectedOutcome: Partial<Record<string, ActionStatus>> = {
    APPLIED: 'APPLIED',
    VERIFIED: 'VERIFIED',
    FAILED: 'FAILED',
    ROLLED_BACK: 'REVERTED',
    SIMULATED: 'SIMULATED',
  };
  if (state in expectedOutcome && outcome?.status !== expectedOutcome[state]) {
    throw new Error('control state contradicts its action outcome');
  }
  if (
    state === 'ROLLBACK_REQUESTED' &&
    outcome?.status !== 'APPLIED' &&
    outcome?.status !== 'VERIFIED'
  ) {
    throw new Error('rollback request has no server-held effect in force');
  }
  if (
    !['APPLIED', 'VERIFIED', 'FAILED', 'ROLLED_BACK', 'SIMULATED', 'ROLLBACK_REQUESTED'].includes(
      state,
    ) &&
    outcome !== null
  ) {
    throw new Error('control state cannot carry an action outcome');
  }
  if (Number(plan.body.estimated_blast_fraction) > Number(rung.maximum_blast_fraction)) {
    throw new Error('plan blast radius exceeds its rung');
  }
  return body as unknown as ActionControlSnapshot;
}

export function parseActionControlResponse(value: unknown): ActionControlResponse {
  const body = record(value, 'action-control response');
  exact(body, ['status', 'control', 'message'], 'action-control response');
  if (body.status !== 'ready' && body.status !== 'not_found' && body.status !== 'degraded') {
    throw new Error('action-control response status is unknown');
  }
  const control = body.control === null ? null : parseControl(body.control);
  const message = body.message === null ? null : text(body.message, 'action-control message');
  if (
    (body.status === 'ready' && (control === null || message !== null)) ||
    (body.status !== 'ready' && (control !== null || message === null))
  ) {
    throw new Error('action-control response status contradicts its payload');
  }
  return { status: body.status, control, message };
}

function headers(credential: string | null): HeadersInit {
  return credential === null ? {} : { 'x-sentinel-secret': credential };
}

async function parseResponse(response: Response): Promise<ActionControlResponse> {
  if (response.status === 401) throw new CredentialRequiredError();
  if (![200, 404, 409, 503].includes(response.status)) {
    throw new Error(`action-control request failed: ${response.status}`);
  }
  return parseActionControlResponse(await response.json());
}

export async function fetchActionControl(
  incidentId: string,
  credential: string | null,
  signal?: AbortSignal,
): Promise<ActionControlResponse> {
  return parseResponse(
    await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/action`, {
      headers: headers(credential),
      ...(signal ? { signal } : {}),
    }),
  );
}

export async function mutateActionControl(
  incidentId: string,
  planRevision: number,
  intent: ActionControlIntent,
  credential: string | null,
): Promise<ActionControlResponse> {
  return parseResponse(
    await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/action`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...headers(credential),
      },
      body: JSON.stringify({
        incident_id: incidentId,
        plan_revision: planRevision,
        intent,
      }),
    }),
  );
}
