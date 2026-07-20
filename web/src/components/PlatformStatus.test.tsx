import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PlatformStatus } from '@/components/PlatformStatus';

const VERSION = {
  service: 'sentinel-gateway',
  version: '0.1.0',
  git_sha: '6d438319e8d9ca1abf96ca2ac01f83c4180b5d30',
  short_sha: '6d43831',
  built_at: '2026-07-20T09:00:00Z',
  env: 'dev',
};

function stubGateway(health: unknown, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const body = url.includes('/api/health') ? health : VERSION;
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: url.includes('/api/health') ? status : 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('PlatformStatus', () => {
  it('reports every plane ready and stamps the running commit', async () => {
    stubGateway({
      status: 'ready',
      degraded: [],
      components: [{ name: 'postgres', ready: true, latency_ms: 1.2, detail: null }],
    });

    render(<PlatformStatus />);

    expect(await screen.findByText('All planes ready.')).toBeInTheDocument();
    expect(await screen.findByText(/6d43831/)).toBeInTheDocument();
  });

  it('names the degraded dependency when the gateway answers 503', async () => {
    stubGateway(
      {
        status: 'degraded',
        degraded: ['loki'],
        components: [
          { name: 'loki', ready: false, latency_ms: 2000, detail: 'timed out after 2s' },
        ],
      },
      503,
    );

    render(<PlatformStatus />);

    expect(await screen.findByText('Degraded: loki')).toBeInTheDocument();
  });

  it('says so plainly when the gateway cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('connection refused'))));

    render(<PlatformStatus />);

    expect(await screen.findByText('Gateway unreachable')).toBeInTheDocument();
  });
});
