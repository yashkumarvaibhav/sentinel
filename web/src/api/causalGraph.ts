import type {
  CausalGraph,
  CausalGraphEdge,
  CausalGraphNode,
  CausalGraphResponse,
  IncidentState,
  SymptomKind,
} from '@/contracts/types';

const STATES = new Set<IncidentState>(['OPEN', 'MITIGATING', 'MONITORING', 'RESOLVED']);
const TIERS = new Set<CausalGraphNode['tier']>([
  'edge',
  'application',
  'data',
  'infrastructure',
]);
const CRITICALITIES = new Set<CausalGraphNode['criticality']>([
  'low',
  'medium',
  'high',
  'critical',
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

function record(value: unknown, name: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exact(
  body: Record<string, unknown>,
  expected: readonly string[],
  name: string,
): void {
  const allowed = new Set(expected);
  const unknown = Object.keys(body).filter((key) => !allowed.has(key));
  if (unknown.length > 0) {
    throw new Error(`${name} has unknown field: ${unknown.sort().join(', ')}`);
  }
  const missing = expected.filter((key) => !(key in body));
  if (missing.length > 0) {
    throw new Error(`${name} is missing field: ${missing.join(', ')}`);
  }
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

function member<T extends string>(value: unknown, values: Set<T>, name: string): T {
  if (typeof value !== 'string' || !values.has(value as T)) {
    throw new Error(`${name} is unknown`);
  }
  return value as T;
}

function bool(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${name} must be boolean`);
  return value;
}

function probability(value: unknown, name: string): number {
  if (
    typeof value !== 'number' ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 1
  ) {
    throw new Error(`${name} must be within [0, 1]`);
  }
  return value;
}

function count(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative integer`);
  }
  return value;
}

function optionalText(value: unknown, name: string): string | null {
  return value === null ? null : text(value, name);
}

function optionalProbability(value: unknown, name: string): number | null {
  return value === null ? null : probability(value, name);
}

function uniqueTextArray(value: unknown, name: string): string[] {
  if (!Array.isArray(value)) throw new Error(`${name} must be an array`);
  const values = value.map((item) => text(item, name));
  if (new Set(values).size !== values.length) throw new Error(`${name} must be unique`);
  return values;
}

function parseNode(value: unknown): CausalGraphNode {
  const body = record(value, 'causal graph node');
  exact(
    body,
    [
      'service',
      'tier',
      'criticality',
      'symptom_heat',
      'active_episode_count',
      'symptom_kinds',
      'is_origin',
      'origin_confidence',
      'implicated',
      'note',
    ],
    'causal graph node',
  );
  if (!Array.isArray(body.symptom_kinds)) {
    throw new Error('causal graph symptom kinds must be an array');
  }
  const symptomKinds = body.symptom_kinds.map((kind) =>
    member(kind, KINDS, 'causal graph symptom kind'),
  );
  if (new Set(symptomKinds).size !== symptomKinds.length) {
    throw new Error('causal graph symptom kinds must be unique');
  }
  const heat = probability(body.symptom_heat, 'causal graph symptom heat');
  const activeCount = count(body.active_episode_count, 'causal graph active episode count');
  const origin = bool(body.is_origin, 'causal graph origin state');
  const originConfidence = optionalProbability(
    body.origin_confidence,
    'causal graph origin confidence',
  );
  const implicated = bool(body.implicated, 'causal graph implication state');
  if (origin !== (originConfidence !== null)) {
    throw new Error('only the causal graph origin may carry origin confidence');
  }
  if (symptomKinds.length === 0 && (heat !== 0 || activeCount !== 0)) {
    throw new Error('a node without member symptoms cannot carry heat or active episodes');
  }
  if (implicated && (symptomKinds.length > 0 || heat !== 0 || activeCount !== 0)) {
    throw new Error('an implicated node cannot claim its own symptom evidence');
  }
  return {
    service: text(body.service, 'causal graph service'),
    tier: member(body.tier, TIERS, 'causal graph tier'),
    criticality: member(
      body.criticality,
      CRITICALITIES,
      'causal graph criticality',
    ),
    symptom_heat: heat,
    active_episode_count: activeCount,
    symptom_kinds: symptomKinds,
    is_origin: origin,
    origin_confidence: originConfidence,
    implicated,
    note: text(body.note, 'causal graph node note'),
  };
}

function parseEdge(value: unknown): CausalGraphEdge {
  const body = record(value, 'causal graph edge');
  exact(
    body,
    [
      'source_service',
      'target_service',
      'active',
      'evidence_episode_ids',
      'note',
    ],
    'causal graph edge',
  );
  const source = text(body.source_service, 'causal graph edge source');
  const target = text(body.target_service, 'causal graph edge target');
  if (source === target) throw new Error('a causal graph edge cannot point to itself');
  const active = bool(body.active, 'causal graph edge active state');
  const evidence = uniqueTextArray(
    body.evidence_episode_ids,
    'causal graph edge evidence ids',
  );
  if (active !== (evidence.length > 0)) {
    throw new Error('only an evidence-backed causal graph edge may be active');
  }
  return {
    source_service: source,
    target_service: target,
    active,
    evidence_episode_ids: evidence,
    note: text(body.note, 'causal graph edge note'),
  };
}

export function parseCausalGraph(
  value: unknown,
  { allowResolved = false }: { allowResolved?: boolean } = {},
): CausalGraph {
  const body = record(value, 'causal graph');
  exact(
    body,
    [
      'incident_id',
      'incident_state',
      'updated_at',
      'honesty',
      'origin_service',
      'origin_confidence',
      'nodes',
      'edges',
    ],
    'causal graph',
  );
  if (!Array.isArray(body.nodes) || body.nodes.length === 0) {
    throw new Error('causal graph nodes must be a non-empty array');
  }
  if (!Array.isArray(body.edges)) throw new Error('causal graph edges must be an array');
  const parsedNodes = body.nodes.map(parseNode);
  const nodes: [CausalGraphNode, ...CausalGraphNode[]] = [
    parsedNodes[0]!,
    ...parsedNodes.slice(1),
  ];
  const edges = body.edges.map(parseEdge);
  const services = nodes.map((node) => node.service);
  if (new Set(services).size !== services.length) {
    throw new Error('causal graph services must be unique');
  }
  const known = new Set(services);
  const edgeIds = edges.map((edge) => `${edge.source_service}\u0000${edge.target_service}`);
  if (new Set(edgeIds).size !== edgeIds.length) {
    throw new Error('causal graph edges must be unique');
  }
  for (const edge of edges) {
    if (!known.has(edge.source_service) || !known.has(edge.target_service)) {
      throw new Error('every causal graph edge endpoint must be a graph node');
    }
  }
  const originService = optionalText(body.origin_service, 'causal graph origin');
  const originConfidence = optionalProbability(
    body.origin_confidence,
    'causal graph origin confidence',
  );
  if ((originService === null) !== (originConfidence === null)) {
    throw new Error('causal graph origin and confidence must appear together');
  }
  const originNodes = nodes.filter((node) => node.is_origin);
  if (
    (originService === null && originNodes.length !== 0) ||
    (originService !== null &&
      (originNodes.length !== 1 ||
        originNodes[0]?.service !== originService ||
        originNodes[0]?.origin_confidence !== originConfidence))
  ) {
    throw new Error('causal graph origin disagrees with its node');
  }
  const incidentState = member(body.incident_state, STATES, 'causal graph incident state');
  if (incidentState === 'RESOLVED' && !allowResolved) {
    throw new Error('a current causal graph cannot describe a resolved incident');
  }
  if (body.honesty !== 'REAL' && body.honesty !== 'SIMULATED') {
    throw new Error('causal graph honesty is unknown');
  }
  return {
    incident_id: text(body.incident_id, 'causal graph incident id'),
    incident_state: incidentState,
    updated_at: time(body.updated_at, 'causal graph update'),
    honesty: body.honesty,
    origin_service: originService,
    origin_confidence: originConfidence,
    nodes,
    edges,
  };
}

export function parseCausalGraphResponse(value: unknown): CausalGraphResponse {
  const body = record(value, 'causal graph response');
  exact(body, ['status', 'graph', 'detail'], 'causal graph response');
  if (body.status !== 'ready' && body.status !== 'empty' && body.status !== 'degraded') {
    throw new Error('causal graph status is unknown');
  }
  const graph = body.graph === null ? null : parseCausalGraph(body.graph);
  const detail = optionalText(body.detail, 'causal graph detail');
  if (
    (body.status === 'ready' && (graph === null || detail !== null)) ||
    (body.status !== 'ready' && (graph !== null || detail === null))
  ) {
    throw new Error('causal graph status contradicts its graph or detail');
  }
  return { status: body.status, graph, detail };
}

export async function fetchCausalGraph(
  signal?: AbortSignal,
): Promise<CausalGraphResponse> {
  const response = await fetch('/api/causal-graph', signal ? { signal } : {});
  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`causal graph request failed: ${response.status}`);
  }
  return parseCausalGraphResponse(await response.json());
}
