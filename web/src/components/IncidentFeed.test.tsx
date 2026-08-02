import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { IncidentFeed } from '@/components/IncidentFeed';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';

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

  fail(): void {
    this.readyState = 0;
    this.onerror?.(new Event('error'));
  }

  emit(resources: string[]): void {
    const event = new MessageEvent('snapshot.invalidate', {
      data: JSON.stringify({
        event_id: `event-${resources.join('-')}`,
        ts: '2026-07-26T03:00:00Z',
        kind: 'snapshot.invalidate',
        resources,
      }),
    });
    for (const listener of this.listeners.get('snapshot.invalidate') ?? []) listener(event);
  }
}

const SNAPSHOT = {
  status: 'ready',
  incidents: [
    {
      incident_id: 'incident-live-1',
      opened_at: '2026-07-26T02:58:00Z',
      updated_at: '2026-07-26T03:00:00Z',
      state: 'OPEN',
      severity: 'HIGH',
      services: ['frontend'],
      origin_service: 'frontend',
      verdict_class: 'ATTACK',
      reason: 'Credential failures concentrated into machine-regular sources.',
      evidence: [
        {
          feature: 'source.entropy',
          value: 0.12,
          baseline: 0.91,
          direction: 'BELOW_BASELINE',
          note: 'Source entropy fell below its measured baseline.',
        },
        {
          feature: 'auth.failure_ratio',
          value: 0.74,
          baseline: 0.05,
          direction: 'ABOVE_BASELINE',
          note: 'Authentication failures rose above baseline.',
        },
      ],
      action: {
        decision_action: 'ALERT',
        effect_status: null,
        detail: 'Alert raised; no production effect was requested.',
      },
      confidence: {
        status: 'insufficient',
        value: null,
        note: 'No calibrated runtime confidence is attached.',
      },
      muted: false,
      explanation: null,
      honesty: 'REAL',
    },
  ],
  count: 1,
  limit: 20,
  observation: {
    status: 'WATCHING',
    last_judged_at: '2026-07-26T12:00:00Z',
    age_seconds: 2,
    expected_within_seconds: 120,
    note: 'Live telemetry is being judged now.',
  },
  detail: null,
};

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});

afterEach(() => vi.unstubAllGlobals());

describe('IncidentFeed', () => {
  it('renders evidence-first live cards and resnapshots on incident invalidation', async () => {
    const fetcher = vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify(SNAPSHOT), { status: 200 })),
    );
    vi.stubGlobal('fetch', fetcher);

    render(
      <MemoryRouter>
        <SnapshotStreamProvider>
          <IncidentFeed />
        </SnapshotStreamProvider>
      </MemoryRouter>,
    );
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() =>
      expect(screen.getByRole('article', { name: /attack incident/i })).toBeInTheDocument(),
    );
    expect(screen.getByText(/source\.entropy/i)).toBeInTheDocument();
    expect(screen.getByText(/auth\.failure_ratio/i)).toBeInTheDocument();
    expect(screen.getByText(/insufficient confidence/i)).toBeInTheDocument();
    expect(screen.getByText('REAL')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /open evidence proof/i })).toHaveAttribute(
      'href',
      '/incidents/incident-live-1',
    );
    expect(screen.getByRole('feed')).toHaveAttribute('aria-live', 'polite');

    const before = fetcher.mock.calls.length;
    act(() => FakeEventSource.instances[0]?.emit(['incidents']));
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThan(before));

    const beforeReconnect = fetcher.mock.calls.length;
    act(() => {
      FakeEventSource.instances[0]?.fail();
      FakeEventSource.instances[0]?.open();
    });
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThan(beforeReconnect));
  });

  it('keeps an empty live store distinct from proof of a quiet system', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ ...SNAPSHOT, incidents: [], count: 0 }),
            { status: 200 },
          ),
        ),
      ),
    );

    render(
      <MemoryRouter>
        <SnapshotStreamProvider>
          <IncidentFeed />
        </SnapshotStreamProvider>
      </MemoryRouter>,
    );
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() =>
      expect(screen.getByText(/no live incidents have been persisted/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/not a zero-risk claim/i)).toBeInTheDocument();
    expect(screen.queryByRole('feed')).not.toBeInTheDocument();
  });

  it('says plainly when nothing is being measured, so stale cards cannot read as calm', async () => {
    const stale = {
      ...SNAPSHOT,
      observation: {
        status: 'STALE' as const,
        last_judged_at: '2026-07-26T11:00:00Z',
        age_seconds: 3600,
        expected_within_seconds: 120,
        note: 'Nothing has been judged for longer than the expected interval, so everything below is the last state measured, not the current one.',
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(stale), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        ),
      ),
    );

    render(
      <MemoryRouter>
        <SnapshotStreamProvider>
          <IncidentFeed />
        </SnapshotStreamProvider>
      </MemoryRouter>,
    );
    act(() => FakeEventSource.instances[0]?.open());

    const banner = await screen.findByTestId('observation');
    expect(banner).toHaveTextContent(/not currently watching/i);
    expect(banner).toHaveTextContent(/last state measured, not the current one/i);
    expect(banner).toHaveTextContent(/last judged 60 min ago/i);
    // The cards are still shown — they are the last known state, not a lie.
    expect(await screen.findByRole('article', { name: /attack incident/i })).toBeInTheDocument();
  });

  it('a feed that has never observed anything refuses to imply safety', async () => {
    const never = {
      ...SNAPSHOT,
      incidents: [],
      count: 0,
      observation: {
        status: 'NEVER' as const,
        last_judged_at: null,
        age_seconds: null,
        expected_within_seconds: 120,
        note: 'No live telemetry has ever been judged here. An empty feed is not evidence that nothing is wrong.',
      },
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(never), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        ),
      ),
    );

    render(
      <MemoryRouter>
        <SnapshotStreamProvider>
          <IncidentFeed />
        </SnapshotStreamProvider>
      </MemoryRouter>,
    );
    act(() => FakeEventSource.instances[0]?.open());

    const banner = await screen.findByTestId('observation');
    expect(banner).toHaveTextContent(/never observed/i);
    expect(banner).toHaveTextContent(/not evidence that nothing is wrong/i);
  });
});
