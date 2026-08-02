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

  // The theme control stopped being a three-state cycle behind one button and
  // became a menu (decision #142). The behaviour these assert is unchanged and
  // still asserted in full - every preference is reachable, and `system` writes
  // no attribute - but a cycle has no "pick dark directly", so the shape of the
  // interaction moved with the control.
  it('reaches every theme directly, without cycling past the others', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    await screen.findByText('abc123d');

    // `system` writes no attribute at all: a user who has never chosen should
    // keep following their OS when it changes at sunset.
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);

    await user.click(screen.getByRole('button', { name: /theme/i }));
    // One click from `system`, where the old cycle needed two.
    await user.click(screen.getByRole('menuitemradio', { name: /dark/i }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    await user.click(screen.getByRole('button', { name: /theme/i }));
    await user.click(screen.getByRole('menuitemradio', { name: /light/i }));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    await user.click(screen.getByRole('button', { name: /theme/i }));
    await user.click(screen.getByRole('menuitemradio', { name: /system/i }));
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
  });

  it('marks the theme it is currently on, so nothing has to be inferred', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    // Settle the build-stamp fetch before asserting, so its resolution does not
    // land as an un-acted update after the test body has finished.
    await screen.findByText('abc123d');

    await user.click(screen.getByRole('button', { name: /theme/i }));

    expect(screen.getByRole('menuitemradio', { name: /system/i })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('menuitemradio', { name: /dark/i })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('closes the theme menu on Escape and returns focus to its trigger', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    await screen.findByText('abc123d');
    const trigger = screen.getByRole('button', { name: /theme/i });

    await user.click(trigger);
    expect(screen.getByRole('menu')).toBeInTheDocument();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('shows both audiences at once and marks the selected one', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    await screen.findByText('abc123d');

    // Both options are on screen, which is the whole point: a single button
    // captioned with the audience you are already in reads as "click for this".
    const technical = screen.getByRole('radio', { name: /technical view/i });
    const exec = screen.getByRole('radio', { name: /exec view/i });
    expect(technical).toHaveAttribute('aria-checked', 'true');
    expect(exec).toHaveAttribute('aria-checked', 'false');

    await user.click(exec);

    expect(screen.getByRole('radio', { name: /exec view/i })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('radio', { name: /technical view/i })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('names the audience group, because two jargon words alone are not a label', async () => {
    vi.stubGlobal('fetch', respond(READY));

    renderTopBar();
    act(() => FakeEventSource.instances[0]?.open());
    await screen.findByText('abc123d');

    expect(screen.getByRole('radiogroup', { name: 'View' })).toBeInTheDocument();
  });
});
