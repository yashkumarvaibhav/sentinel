import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TopBar } from '@/shell/TopBar';

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

const READY = { status: 'ready', degraded: [], components: [] };
const DEGRADED = { status: 'degraded', degraded: ['loki'], components: [] };

beforeEach(() => {
  document.documentElement.removeAttribute('data-theme');
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('TopBar', () => {
  it('announces the connection state rather than only colouring a dot', async () => {
    vi.stubGlobal('fetch', respond(READY));

    render(<TopBar />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/every component ready/i);
    });
  });

  it('says which component is degraded, not merely that something is', async () => {
    // A 503 with a full body is the platform successfully telling us something
    // is wrong - the opposite of not answering - so it must not read as down.
    vi.stubGlobal('fetch', respond(DEGRADED, 503));

    render(<TopBar />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/loki/);
    });
    expect(screen.getByRole('status')).not.toHaveTextContent(/not answering/i);
  });

  it('reports the platform as down when it does not answer at all', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('connection refused'))),
    );

    render(<TopBar />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/not answering/i);
    });
  });

  it('shows the commit that is actually serving', async () => {
    vi.stubGlobal('fetch', respond(READY));

    render(<TopBar />);

    await waitFor(() => {
      expect(screen.getByText('abc123d')).toBeInTheDocument();
    });
  });

  it('cycles the theme through system, light and dark', async () => {
    vi.stubGlobal('fetch', respond(READY));
    const user = userEvent.setup();

    render(<TopBar />);
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

    render(<TopBar />);
    const toggle = screen.getByRole('button', { name: 'Technical' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');

    await user.click(toggle);

    const pressed = screen.getByRole('button', { name: 'Exec' });
    expect(pressed).toHaveAttribute('aria-pressed', 'true');
  });
});
