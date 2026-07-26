import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HealthReport } from '@/api/platform';
import { TopBar } from '@/shell/TopBar';
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

  fail({ terminal = false }: { terminal?: boolean } = {}): void {
    this.readyState = terminal ? 2 : 0;
    this.onerror?.(new Event('error'));
  }

  emit(data: string): void {
    const event = new MessageEvent('snapshot.invalidate', { data });
    for (const listener of this.listeners.get('snapshot.invalidate') ?? []) {
      listener(event);
    }
  }
}

function respond(health: unknown, status = 200) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input.toString();
    if (url.endsWith('/api/health')) {
      return Promise.resolve(new Response(JSON.stringify(health), { status }));
    }
    return Promise.resolve(
      new Response(
        JSON.stringify({
          service: 'sentinel-gateway',
          version: '0.1.0',
          git_sha: 'abc123def456',
          short_sha: 'abc123d',
          built_at: '2026-07-26T00:00:00Z',
          env: 'dev',
        }),
        { status: 200 },
      ),
    );
  });
}

const READY: HealthReport = { status: 'ready', degraded: [], components: [] };
const DEGRADED: HealthReport = { status: 'degraded', degraded: ['loki'], components: [] };

function renderTopBar() {
  return render(
    <SnapshotStreamProvider>
      <TopBar />
    </SnapshotStreamProvider>,
  );
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  document.documentElement.removeAttribute('data-theme');
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('TopBar', () => {
  it('announces the connection state rather than only colouring a dot', async () => {
    vi.stubGlobal('fetch', respond(READY));

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/every component ready/i);
    });
    expect(screen.getByRole('status').parentElement).toHaveAttribute(
      'title',
      expect.stringMatching(/SSE live; snapshots refetch after reconnect/i),
    );
  });

  it('says which component is degraded, not merely that something is', async () => {
    // A 503 with a full body is the platform successfully telling us something
    // is wrong - the opposite of not answering - so it must not read as down.
    vi.stubGlobal('fetch', respond(DEGRADED, 503));

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/loki/);
    });
    expect(screen.getByRole('status')).not.toHaveTextContent(/not answering/i);
  });

  it('keeps an open stream distinct from a failed health snapshot', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('connection refused'))),
    );

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/stream open/i);
      expect(screen.getByRole('status')).toHaveTextContent(/connection refused/i);
    });
  });

  it('announces automatic recovery when the event stream disconnects', async () => {
    vi.stubGlobal('fetch', respond(READY));

    renderTopBar();
    const source = FakeEventSource.instances[0];
    act(() => source?.open());
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/ready/i));

    act(() => source?.fail());

    expect(screen.getByRole('status')).toHaveTextContent(/reconnecting automatically/i);
  });

  it('refetches component health when the stream invalidates that snapshot', async () => {
    let health = READY;
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = input instanceof Request ? input.url : input.toString();
        return Promise.resolve(
          new Response(
            JSON.stringify(
              url.endsWith('/api/health')
                ? health
                : {
                    service: 'sentinel-gateway',
                    version: '0.1.0',
                    git_sha: 'abc123def456',
                    short_sha: 'abc123d',
                    built_at: '2026-07-26T00:00:00Z',
                    env: 'dev',
                  },
            ),
            { status: url.endsWith('/api/health') && health === DEGRADED ? 503 : 200 },
          ),
        );
      }),
    );

    renderTopBar();
    const source = FakeEventSource.instances[0];
    act(() => source?.open());
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/ready/i));

    health = DEGRADED;
    act(() =>
      source?.emit(
        JSON.stringify({
          event_id: 'health-2',
          ts: '2026-07-26T00:00:15Z',
          kind: 'snapshot.invalidate',
          resources: ['health'],
        }),
      ),
    );

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/loki/i));
  });

  it('reports a stream that closes permanently as unavailable', async () => {
    vi.stubGlobal('fetch', respond(READY));

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.fail({ terminal: true }));

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/stream unavailable/i),
    );
  });

  it('shows the commit that is actually serving', async () => {
    vi.stubGlobal('fetch', respond(READY));

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());

    await waitFor(() => {
      expect(screen.getByText('abc123d')).toBeInTheDocument();
    });
  });

  it('cycles the theme through system, light and dark', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    const toggle = screen.getByRole('button', { name: /theme/i });

    // `system` writes no attribute at all: a user who has never chosen should
    // keep following their OS when it changes at sunset.
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);

    await user.click(toggle);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    await user.click(toggle);
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    await user.click(toggle);
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
  });

  it('toggles the audience and says which one is pressed', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    const toggle = screen.getByRole('button', { name: 'Technical' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');

    await user.click(toggle);

    const pressed = screen.getByRole('button', { name: 'Exec' });
    expect(pressed).toHaveAttribute('aria-pressed', 'true');
  });
});
