import { act, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CausalGraphPanel } from '@/components/CausalGraph';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';

vi.mock('@xyflow/react', () => ({
  Background: () => null,
  Handle: () => null,
  MarkerType: { ArrowClosed: 'arrowclosed' },
  Panel: () => null,
  Position: { Top: 'top', Right: 'right', Bottom: 'bottom', Left: 'left' },
  ReactFlow: ({
    nodes,
    edges,
  }: {
    nodes: Array<{ id: string; ariaLabel?: string; data: { label?: ReactNode } }>;
    edges: Array<{ id: string; animated?: boolean; ariaLabel?: string }>;
  }) => (
    <div aria-label="Causal topology canvas">
      {nodes.map((node) => (
        <button aria-label={node.ariaLabel} key={node.id} type="button">
          {node.ariaLabel}
        </button>
      ))}
      {edges.map((edge) => (
        <span
          data-animated={String(edge.animated)}
          data-testid={`edge-${edge.id}`}
          key={edge.id}
        >
          {edge.ariaLabel}
        </span>
      ))}
    </div>
  ),
}));

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(readonly url: string | URL) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener): void {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.readyState = 2;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  emit(): void {
    const event = new MessageEvent('snapshot.invalidate', {
      data: JSON.stringify({
        event_id: 'graph-invalidation',
        ts: '2026-07-26T03:00:00Z',
        kind: 'snapshot.invalidate',
        resources: ['incidents'],
      }),
    });
    for (const listener of this.listeners.get('snapshot.invalidate') ?? []) listener(event);
  }
}

const READY = {
  status: 'ready',
  graph: {
    incident_id: 'incident-live-1',
    incident_state: 'OPEN',
    updated_at: '2026-07-26T03:00:00Z',
    honesty: 'REAL',
    origin_service: 'payment',
    origin_confidence: 0.88,
    nodes: [
      {
        service: 'checkout',
        tier: 'application',
        criticality: 'critical',
        symptom_heat: 0.81,
        active_episode_count: 1,
        symptom_kinds: ['EDGE_DEGRADED'],
        is_origin: false,
        origin_confidence: null,
        implicated: false,
        note: 'One active edge degradation episode.',
      },
      {
        service: 'payment',
        tier: 'application',
        criticality: 'critical',
        symptom_heat: 0,
        active_episode_count: 0,
        symptom_kinds: [],
        is_origin: true,
        origin_confidence: 0.88,
        implicated: true,
        note: 'Collapsed origin implicated by dependency evidence.',
      },
    ],
    edges: [
      {
        source_service: 'payment',
        target_service: 'checkout',
        active: true,
        evidence_episode_ids: ['edge-1'],
        note: 'Measured propagation from dependency to caller.',
      },
    ],
  },
  detail: null,
};

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      matches: true,
      media: '(prefers-reduced-motion: reduce)',
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe('CausalGraphPanel', () => {
  it('renders origin, heat, textual propagation evidence, and reduced-motion-safe edges', async () => {
    const fetcher = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify(READY), { status: 200 })),
    );
    vi.stubGlobal('fetch', fetcher);

    render(
      <SnapshotStreamProvider>
        <CausalGraphPanel />
      </SnapshotStreamProvider>,
    );
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /causal chain/i })).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /payment.*collapsed origin/i })).toBeInTheDocument();
    expect(screen.getByText(/81% peak symptom heat/i)).toBeInTheDocument();
    expect(screen.getByText(/measured propagation: payment → checkout/i)).toBeInTheDocument();
    expect(screen.getByText('REAL')).toBeInTheDocument();
    expect(screen.getByTestId(/edge-/)).toHaveAttribute('data-animated', 'false');

    const before = fetcher.mock.calls.length;
    act(() => FakeEventSource.instances[0]?.emit());
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThan(before));
  });

  it('states that no graph is not evidence of health', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              status: 'empty',
              graph: null,
              detail: 'No current incident has an evidence-backed causal graph.',
            }),
            { status: 200 },
          ),
        ),
      ),
    );

    render(
      <SnapshotStreamProvider>
        <CausalGraphPanel />
      </SnapshotStreamProvider>,
    );
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() =>
      expect(screen.getByText(/no current causal graph/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/not proof that the topology is healthy/i)).toBeInTheDocument();
  });
});
