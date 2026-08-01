import type { LabRunFeed, LabRunMode, LabRunSnapshot, LabScenarioOption } from '@/contracts/types';

/**
 * The launcher is a sensitive surface, so a missing credential is a distinct
 * outcome rather than an error string: the page has something specific to say
 * about it, and it is not "something went wrong".
 */
export class LabCredentialRequiredError extends Error {
  constructor() {
    super('The demo launcher requires the shared secret.');
    this.name = 'LabCredentialRequiredError';
  }
}

const MODES = new Set<LabRunMode>(['REPLAY', 'LIVE']);
const STATES = new Set<LabRunSnapshot['state']>([
  'QUEUED',
  'RUNNING',
  'SUCCEEDED',
  'FAILED',
  'REFUSED',
]);
const FEED_STATUSES = new Set<LabRunFeed['status']>(['READY', 'BUSY', 'UNAVAILABLE']);

function record(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, what: string): string {
  if (typeof value !== 'string' || value.trim() === '') throw new Error(`${what} must be text`);
  return value;
}

function nullableText(value: unknown, what: string): string | null {
  return value === null ? null : text(value, what);
}

function member<T>(value: unknown, allowed: ReadonlySet<T>, what: string): T {
  if (typeof value !== 'string' || !allowed.has(value as T)) {
    throw new Error(`${what} is not a value this build understands`);
  }
  return value as T;
}

function parseRun(value: unknown): LabRunSnapshot {
  const body = record(value, 'lab run');
  const state = member(body.state, STATES, 'lab run state');
  const finishedAt = nullableText(body.finished_at, 'lab run finished_at');
  const terminal = state === 'SUCCEEDED' || state === 'FAILED' || state === 'REFUSED';
  // The same coherence the server contract enforces, checked again here. A
  // payload claiming a run is still going while stating when it ended is
  // precisely the confusion these fields exist to prevent.
  if (terminal !== (finishedAt !== null)) {
    throw new Error('a finished time and a terminal run state must travel together');
  }
  const honesty = record(body.honesty, 'lab run honesty');
  return {
    run_id: text(body.run_id, 'lab run id'),
    scenario_id: text(body.scenario_id, 'lab run scenario'),
    mode: member(body.mode, MODES, 'lab run mode'),
    state,
    requested_at: text(body.requested_at, 'lab run requested_at'),
    started_at: nullableText(body.started_at, 'lab run started_at'),
    finished_at: finishedAt,
    incident_id: nullableText(body.incident_id, 'lab run incident'),
    detail: text(body.detail, 'lab run detail'),
    honesty: {
      telemetry: text(honesty.telemetry, 'lab run telemetry honesty'),
      stimulus: text(honesty.stimulus, 'lab run stimulus honesty'),
      reproducibility: text(honesty.reproducibility, 'lab run reproducibility'),
    },
  };
}

function parseScenario(value: unknown): LabScenarioOption {
  const body = record(value, 'lab scenario');
  const modes = Array.isArray(body.modes) ? body.modes : [];
  if (modes.length === 0) throw new Error('a scenario must offer at least one mode');
  const walked = modes.map((mode) => member(mode, MODES, 'lab scenario mode'));
  const duration = body.live_duration_seconds;
  const parsed: [LabRunMode, ...LabRunMode[]] = [walked[0] as LabRunMode, ...walked.slice(1)];
  const live = parsed.includes('LIVE');
  if (live !== (typeof duration === 'number')) {
    throw new Error('a live-capable scenario must state how long live takes, and only then');
  }
  return {
    scenario_id: text(body.scenario_id, 'lab scenario id'),
    name: text(body.name, 'lab scenario name'),
    description: text(body.description, 'lab scenario description'),
    modes: parsed,
    ...(live ? { live_duration_seconds: duration as number } : {}),
  };
}

export function parseLabFeed(value: unknown): LabRunFeed {
  const body = record(value, 'lab feed');
  const status = member(body.status, FEED_STATUSES, 'lab feed status');
  const runnerAttached = body.runner_attached;
  if (typeof runnerAttached !== 'boolean') {
    throw new Error('the launcher must say whether a runner is attached');
  }
  if (status === 'READY' && !runnerAttached) {
    throw new Error('a launcher with no runner attached cannot be ready');
  }
  return {
    status,
    scenarios: (Array.isArray(body.scenarios) ? body.scenarios : []).map(parseScenario),
    runs: (Array.isArray(body.runs) ? body.runs : []).map(parseRun),
    runner_attached: runnerAttached,
    note: text(body.note, 'lab feed note'),
  };
}

function headers(credential: string | null): HeadersInit | undefined {
  return credential === null ? undefined : { 'x-sentinel-secret': credential };
}

export async function fetchLabFeed(
  credential: string | null,
  signal?: AbortSignal,
): Promise<LabRunFeed> {
  const init: RequestInit = {};
  const auth = headers(credential);
  if (auth !== undefined) init.headers = auth;
  if (signal !== undefined) init.signal = signal;
  const response = await fetch('/api/lab/runs', init);
  if (response.status === 401) throw new LabCredentialRequiredError();
  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`Lab run feed request failed (${response.status})`);
  }
  return parseLabFeed(await response.json());
}

export async function fireScenario(
  credential: string | null,
  request: { scenario_id: string; mode: LabRunMode },
  signal?: AbortSignal,
): Promise<LabRunFeed> {
  const auth = headers(credential);
  const init: RequestInit = {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...(auth ?? {}) },
    body: JSON.stringify(request),
  };
  if (signal !== undefined) init.signal = signal;
  const response = await fetch('/api/lab/scenario', init);
  if (response.status === 401) throw new LabCredentialRequiredError();
  // 409 is a real answer, not a failure: something is already running, and the
  // body says which. 400 likewise names a scenario this deployment will not fire.
  if (![200, 400, 409, 503].includes(response.status)) {
    throw new Error(`Firing a scenario failed (${response.status})`);
  }
  return parseLabFeed(await response.json());
}
