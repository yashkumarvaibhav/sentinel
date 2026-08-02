import {
  Background,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  useReactFlow,
} from '@xyflow/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import '@xyflow/react/dist/style.css';

import { fetchCausalGraph } from '@/api/causalGraph';
import type {
  CausalGraph,
  CausalGraphNode,
  CausalGraphResponse,
} from '@/contracts/types';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

type Load =
  | { state: 'loading' }
  | { state: 'ready'; response: CausalGraphResponse }
  | { state: 'error'; detail: string };

const INCIDENT_RESOURCES = ['incidents'] as const;

function words(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () =>
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia(query).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(query);
    const update = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [query]);
  return matches;
}

function compactOrder(graph: CausalGraph): string[] {
  const services = graph.nodes.map((node) => node.service).sort();
  if (graph.origin_service === null) return services;
  const outgoing = new Map(services.map((service) => [service, [] as string[]]));
  for (const edge of graph.edges) {
    outgoing.get(edge.source_service)?.push(edge.target_service);
  }
  const queue = [graph.origin_service];
  const seen = new Set<string>();
  const ordered: string[] = [];
  let cursor = 0;
  while (cursor < queue.length) {
    const service = queue[cursor++];
    if (service === undefined || seen.has(service)) continue;
    seen.add(service);
    ordered.push(service);
    queue.push(...[...(outgoing.get(service) ?? [])].sort());
  }
  ordered.push(...services.filter((service) => !seen.has(service)));
  return ordered;
}

function layout(
  graph: CausalGraph,
  compact: boolean,
): Map<string, { x: number; y: number }> {
  if (compact) {
    return new Map(
      compactOrder(graph).map((service, index) => [
        service,
        { x: 0, y: index * 170 },
      ]),
    );
  }
  const services = graph.nodes.map((node) => node.service).sort();
  const ranks = new Map(services.map((service) => [service, 0]));
  const indegree = new Map(services.map((service) => [service, 0]));
  const outgoing = new Map(services.map((service) => [service, [] as string[]]));
  for (const edge of graph.edges) {
    indegree.set(edge.target_service, (indegree.get(edge.target_service) ?? 0) + 1);
    outgoing.get(edge.source_service)?.push(edge.target_service);
  }
  const queue = services.filter((service) => indegree.get(service) === 0);
  let cursor = 0;
  while (cursor < queue.length) {
    const source = queue[cursor++];
    if (source === undefined) break;
    const targets = [...(outgoing.get(source) ?? [])].sort();
    for (const target of targets) {
      ranks.set(target, Math.max(ranks.get(target) ?? 0, (ranks.get(source) ?? 0) + 1));
      const remaining = (indegree.get(target) ?? 1) - 1;
      indegree.set(target, remaining);
      if (remaining === 0) queue.push(target);
    }
  }
  const acyclicMax = Math.max(0, ...ranks.values());
  for (const service of services.filter((item) => (indegree.get(item) ?? 0) > 0)) {
    ranks.set(service, acyclicMax + 1);
  }
  const byRank = new Map<number, string[]>();
  for (const service of services) {
    const rank = ranks.get(service) ?? 0;
    byRank.set(rank, [...(byRank.get(rank) ?? []), service]);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [rank, ranked] of [...byRank.entries()].sort(([left], [right]) => left - right)) {
    ranked.sort().forEach((service, index) => {
      positions.set(service, { x: rank * 270, y: index * 170 });
    });
  }
  return positions;
}

function nodeLabel(node: CausalGraphNode) {
  return (
    <div className="flex min-w-48 flex-col gap-2 text-left">
      <div className="flex items-start justify-between gap-2">
        <strong className="text-sm">{node.service}</strong>
        <span className="text-[10px] tracking-wide uppercase">{node.criticality}</span>
      </div>
      <div className="flex flex-wrap gap-1 text-[10px]">
        {node.is_origin && node.origin_confidence !== null && (
          <span className="bg-accent-soft text-accent-hover rounded px-1.5 py-0.5">
            Origin {percent(node.origin_confidence)}
          </span>
        )}
        {node.implicated && (
          <span className="border-line rounded border px-1.5 py-0.5">Implicated</span>
        )}
        <span className="border-line rounded border px-1.5 py-0.5">
          {percent(node.symptom_heat)} peak heat
        </span>
        <span className="border-line rounded border px-1.5 py-0.5">
          {node.active_episode_count} active
        </span>
      </div>
      <span className="text-muted text-[10px]">{node.tier}</span>
    </div>
  );
}

function nodeStyle(node: CausalGraphNode): React.CSSProperties {
  const signalColor =
    node.symptom_heat >= 0.75
      ? 'var(--color-residual)'
      : node.symptom_heat >= 0.4
        ? 'var(--color-event)'
        : 'var(--color-decomp-base)';
  return {
    background: 'var(--raised)',
    border: `2px solid ${signalColor}`,
    borderRadius: '0.65rem',
    color: 'var(--ink)',
    padding: '0.75rem',
    width: 220,
    height: 140,
    boxShadow: node.is_origin
      ? '0 0 0 4px var(--accent), 0 8px 20px rgba(0, 0, 0, 0.12)'
      : '0 4px 14px rgba(0, 0, 0, 0.08)',
  };
}

type CausalFlowNode = Node<
  { node: CausalGraphNode; compact: boolean },
  'causal'
>;

function CausalNodeView({ data }: NodeProps<CausalFlowNode>) {
  return (
    <div style={nodeStyle(data.node)}>
      <Handle
        isConnectable={false}
        position={data.compact ? Position.Top : Position.Left}
        type="target"
      />
      {nodeLabel(data.node)}
      <Handle
        isConnectable={false}
        position={data.compact ? Position.Bottom : Position.Right}
        type="source"
      />
    </div>
  );
}

const NODE_TYPES = { causal: CausalNodeView };

function GraphControls() {
  const { fitView, zoomIn, zoomOut } = useReactFlow();
  return (
    <Panel position="bottom-left">
      <div
        aria-label="Graph view controls"
        className="sentinel-graph-controls flex flex-col"
        role="group"
      >
        <button
          aria-label="Zoom causal graph in"
          onClick={() => void zoomIn()}
          type="button"
        >
          +
        </button>
        <button
          aria-label="Zoom causal graph out"
          onClick={() => void zoomOut()}
          type="button"
        >
          −
        </button>
        <button
          aria-label="Fit the full causal graph in view"
          onClick={() => void fitView({ padding: 0.18 })}
          type="button"
        >
          Fit
        </button>
      </div>
    </Panel>
  );
}

function flowElements(
  graph: CausalGraph,
  reducedMotion: boolean,
  compact: boolean,
): { nodes: CausalFlowNode[]; edges: Edge[] } {
  const positions = layout(graph, compact);
  return {
    nodes: graph.nodes.map((node) => ({
      id: node.service,
      type: 'causal',
      position: positions.get(node.service) ?? { x: 0, y: 0 },
      data: { node, compact },
      width: 220,
      height: 140,
      handles: [
        {
          id: null,
          type: 'target',
          position: compact ? Position.Top : Position.Left,
          x: compact ? 110 : 0,
          y: compact ? 0 : 70,
          width: 1,
          height: 1,
        },
        {
          id: null,
          type: 'source',
          position: compact ? Position.Bottom : Position.Right,
          x: compact ? 110 : 220,
          y: compact ? 140 : 70,
          width: 1,
          height: 1,
        },
      ],
      ariaLabel: [
        node.service,
        node.is_origin ? `collapsed origin at ${percent(node.origin_confidence ?? 0)}` : null,
        `${percent(node.symptom_heat)} peak symptom heat`,
        `${node.active_episode_count} active episodes`,
        node.implicated ? 'implicated without own symptom' : null,
      ]
        .filter(Boolean)
        .join(', '),
      draggable: false,
      selectable: true,
    })),
    edges: graph.edges.map((edge) => ({
      id: `${edge.source_service}-${edge.target_service}`,
      source: edge.source_service,
      target: edge.target_service,
      type: 'smoothstep',
      animated: edge.active && !reducedMotion,
      selectable: true,
      focusable: true,
      ariaLabel: edge.active
        ? `Measured propagation from ${edge.source_service} to ${edge.target_service}`
        : `Passive dependency path from ${edge.source_service} to ${edge.target_service}`,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: edge.active ? 'var(--color-residual)' : 'var(--muted)',
      },
      style: {
        stroke: edge.active ? 'var(--color-residual)' : 'var(--muted)',
        strokeWidth: edge.active ? 3 : 1.5,
        opacity: edge.active ? 1 : 0.5,
      },
    })),
  };
}

export function CausalGraphView({ graph }: { graph: CausalGraph }) {
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');
  const compact = useMediaQuery('(max-width: 639px)');
  const elements = useMemo(
    () => flowElements(graph, reducedMotion, compact),
    [compact, graph, reducedMotion],
  );
  const activeEdges = graph.edges.filter((edge) => edge.active);
  return (
    <div className="flex flex-col gap-3" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="border-line text-ink rounded border px-2 py-1 tracking-wider">
          {graph.honesty}
        </span>
        <span className="text-muted">Incident {graph.incident_id}</span>
        <span className="text-muted">· {words(graph.incident_state)}</span>
      </div>
      <div
        aria-label="Causal topology canvas"
        className="border-line bg-sidebar h-[30rem] overflow-hidden rounded-lg border sm:h-[34rem]"
        role="group"
      >
        <ReactFlow
          autoPanOnNodeFocus
          className="sentinel-causal-flow"
          defaultViewport={
            compact ? { x: 53, y: 24, zoom: 1 } : { x: 0, y: 0, zoom: 1 }
          }
          edges={elements.edges}
          edgesFocusable
          elementsSelectable
          fitView={!compact}
          fitViewOptions={{ padding: 0.18 }}
          maxZoom={1.5}
          minZoom={0.35}
          nodes={elements.nodes}
          nodesConnectable={false}
          nodesDraggable={false}
          nodesFocusable
          nodeTypes={NODE_TYPES}
          panOnDrag
          zoomOnDoubleClick={false}
        >
          <Background color="var(--line)" gap={22} size={1} />
          <GraphControls />
        </ReactFlow>
      </div>
      <div className="border-line bg-raised rounded-lg border p-4 text-xs">
        <strong className="text-ink">Evidence-readable path</strong>
        {activeEdges.length > 0 ? (
          <ul className="text-body mt-2 grid gap-2">
            {activeEdges.map((edge) => (
              <li key={`${edge.source_service}-${edge.target_service}`}>
                Measured propagation: {edge.source_service} → {edge.target_service}.{' '}
                {edge.evidence_episode_ids.length} member episode
                {edge.evidence_episode_ids.length === 1 ? '' : 's'}.
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted mt-2">
            No member edge episode supports active propagation. Muted arrows are committed
            topology only.
          </p>
        )}
      </div>
    </div>
  );
}

export function CausalGraphPanel() {
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const inFlight = useRef<Promise<void> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const refetchQueued = useRef(false);

  const refetch = useCallback((): Promise<void> => {
    if (inFlight.current !== null) {
      refetchQueued.current = true;
      return inFlight.current;
    }
    const requestController = new AbortController();
    controller.current = requestController;
    const request = fetchCausalGraph(requestController.signal)
      .then((response) => {
        if (!requestController.signal.aborted) {
          setLoad(
            response.status === 'degraded'
              ? { state: 'error', detail: response.detail ?? 'causal graph store unavailable' }
              : { state: 'ready', response },
          );
        }
      })
      .catch((error: unknown) => {
        if (!requestController.signal.aborted) {
          setLoad({
            state: 'error',
            detail: error instanceof Error ? error.message : String(error),
          });
        }
      })
      .finally(() => {
        inFlight.current = null;
        controller.current = null;
        if (refetchQueued.current && !requestController.signal.aborted) {
          refetchQueued.current = false;
          void refetch();
        }
      });
    inFlight.current = request;
    return request;
  }, []);

  useSnapshotInvalidation(INCIDENT_RESOURCES, refetch);
  useEffect(() => {
    void refetch();
    return () => controller.current?.abort();
  }, [refetch]);

  return (
    <section className="flex flex-col gap-4" aria-labelledby="causal-chain">
      <div>
        <h2 id="causal-chain" className="eyebrow font-sans">
          Causal chain
        </h2>
        <p className="text-muted mt-1 text-xs">
          Topology is context. Heat, origin, and active propagation come only from incident
          evidence.
        </p>
      </div>
      {load.state === 'loading' && (
        <p className="text-muted text-sm" role="status">
          Reading the current causal graph…
        </p>
      )}
      {load.state === 'error' && (
        <p className="text-bad text-sm" role="alert">
          Causal graph unavailable: {load.detail}
        </p>
      )}
      {load.state === 'ready' &&
        (load.response.graph === null ? (
          <div className="border-line bg-sidebar rounded-lg border p-4 text-sm">
            <strong>No current causal graph.</strong>
            <p className="text-muted mt-1">
              {load.response.detail} This is not proof that the topology is healthy; no
              unresolved incident graph is currently persisted.
            </p>
          </div>
        ) : (
          <CausalGraphView graph={load.response.graph} />
        ))}
    </section>
  );
}
