import type {
  KpiKey,
  KpiMetric,
  KpiResponse,
  KpiStatus,
  KpiWindow,
  ScoreHeadline,
  ScoreProof,
} from '@/contracts/types';

const KPI_KEYS = [
  'detection_latency',
  'autonomous_mttr',
  'quiet_day_false_acts',
  'protected_cohort_integrity',
] as const satisfies readonly KpiKey[];

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

function samples(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return value;
}

function status(value: unknown, name: string): KpiStatus {
  if (value !== 'ok' && value !== 'insufficient') {
    throw new Error(`${name} must be ok or insufficient`);
  }
  return value;
}

function nullableFinite(value: unknown, name: string): number | null {
  if (value === null) return null;
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`${name} must be a finite number or null`);
  }
  return value;
}

function nullableTime(value: unknown, name: string): string | null {
  if (value === null) return null;
  const rendered = text(value, name);
  if (!Number.isFinite(Date.parse(rendered))) {
    throw new Error(`${name} must be an ISO timestamp or null`);
  }
  return rendered;
}

function parseWindow(value: unknown): KpiWindow {
  const body = record(value, 'KPI window');
  const start = nullableTime(body.start, 'KPI window start');
  const end = nullableTime(body.end, 'KPI window end');
  if ((start === null) !== (end === null)) {
    throw new Error('KPI window start and end must both be present or absent');
  }
  return {
    start,
    end,
    description: text(body.description, 'KPI window description'),
  };
}

function parseMetric(value: unknown, expectedKey: KpiKey): KpiMetric {
  const body = record(value, `KPI ${expectedKey}`);
  if (body.key !== expectedKey) {
    throw new Error(`KPI metrics must be in canonical order; expected ${expectedKey}`);
  }
  const metricStatus = status(body.status, `${expectedKey} status`);
  const metricValue = nullableFinite(body.value, `${expectedKey} value`);
  const sampleCount = samples(body.sample_count, `${expectedKey} sample count`);
  if (metricStatus === 'ok' && (metricValue === null || sampleCount < 1)) {
    throw new Error(`ok KPI ${expectedKey} requires a value and samples`);
  }
  if (metricStatus === 'insufficient' && metricValue !== null) {
    throw new Error(`insufficient KPI ${expectedKey} cannot carry a value`);
  }
  return {
    key: expectedKey,
    label: text(body.label, `${expectedKey} label`),
    definition: text(body.definition, `${expectedKey} definition`),
    status: metricStatus,
    value: metricValue,
    unit: text(body.unit, `${expectedKey} unit`),
    window: parseWindow(body.window),
    sample_count: sampleCount,
    provenance: text(body.provenance, `${expectedKey} provenance`),
  };
}

function parseHeadline(value: unknown): ScoreHeadline {
  const body = record(value, 'score headline');
  const headlineStatus = status(body.status, 'score headline status');
  const headlineValue = nullableFinite(body.value, 'score headline value');
  const sampleCount = samples(body.sample_count, 'score headline sample count');
  if (headlineStatus === 'ok' && (headlineValue === null || sampleCount < 1)) {
    throw new Error('ok score headline requires a value and samples');
  }
  if (headlineStatus === 'insufficient' && headlineValue !== null) {
    throw new Error('insufficient score headline cannot carry a value');
  }
  return {
    key: text(body.key, 'score headline key'),
    label: text(body.label, 'score headline label'),
    status: headlineStatus,
    value: headlineValue,
    unit: text(body.unit, 'score headline unit'),
    sample_count: sampleCount,
  };
}

function parseProof(value: unknown): ScoreProof {
  const body = record(value, 'latest score proof');
  if (body.version !== 1 || (body.gate_status !== 'pass' && body.gate_status !== 'fail')) {
    throw new Error('latest score proof has an unsupported version or gate status');
  }
  if (
    body.telemetry_honesty !== 'REAL' ||
    body.stimulus_honesty !== 'SIMULATED' ||
    body.seed_purpose !== 'held_out'
  ) {
    throw new Error('latest score proof honesty or seed discipline is invalid');
  }
  if (!Array.isArray(body.capture_ids) || body.capture_ids.length === 0) {
    throw new Error('latest score proof requires capture IDs');
  }
  if (!Array.isArray(body.headline_metrics) || body.headline_metrics.length === 0) {
    throw new Error('latest score proof requires headline metrics');
  }
  const captures: [string, ...string[]] = [
    text(body.capture_ids[0], 'capture ID'),
    ...body.capture_ids.slice(1).map((item) => text(item, 'capture ID')),
  ];
  if (new Set(captures).size !== captures.length) {
    throw new Error('latest score proof capture IDs must be unique');
  }
  const headlines: [ScoreHeadline, ...ScoreHeadline[]] = [
    parseHeadline(body.headline_metrics[0]),
    ...body.headline_metrics.slice(1).map(parseHeadline),
  ];
  return {
    version: 1,
    proof_id: text(body.proof_id, 'proof ID'),
    gate_status: body.gate_status,
    evidence_start: text(body.evidence_start, 'proof evidence start'),
    evidence_end: text(body.evidence_end, 'proof evidence end'),
    telemetry_honesty: 'REAL',
    stimulus_honesty: 'SIMULATED',
    seed_purpose: 'held_out',
    capture_ids: captures,
    config_fingerprint: text(body.config_fingerprint, 'config fingerprint'),
    report_path: text(body.report_path, 'score report path'),
    headline_metrics: headlines,
  };
}

export function parseKpiResponse(value: unknown): KpiResponse {
  const body = record(value, 'KPI response');
  if (body.status !== 'ready' && body.status !== 'degraded') {
    throw new Error('KPI response status must be ready or degraded');
  }
  if (!Array.isArray(body.metrics) || body.metrics.length !== KPI_KEYS.length) {
    throw new Error('KPI response must contain exactly four metrics');
  }
  const rawMetrics: unknown[] = body.metrics;
  const metrics = KPI_KEYS.map((key, index) => parseMetric(rawMetrics[index], key));
  const proof = body.latest_score_proof === null ? null : parseProof(body.latest_score_proof);
  if (body.status === 'ready' && proof === null) {
    throw new Error('ready KPI response requires a score proof');
  }
  return {
    status: body.status,
    metrics,
    latest_score_proof: proof,
    detail: body.detail === null || body.detail === undefined ? null : text(body.detail, 'detail'),
  };
}

export async function fetchKpis(signal?: AbortSignal): Promise<KpiResponse> {
  const response = await fetch('/api/kpis', signal ? { signal } : {});
  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`KPI request failed: ${response.status}`);
  }
  return parseKpiResponse(await response.json());
}
