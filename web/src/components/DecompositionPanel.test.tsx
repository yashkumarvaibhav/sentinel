import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DecompositionPanel } from '@/components/DecompositionPanel';

function frame(offset: number, residual: number) {
  return {
    frame_id: `frame-${offset}`,
    observation_id: `observation-${offset}`,
    ts: new Date(Date.UTC(2026, 2, 1, 12, 0, offset)).toISOString(),
    service: 'frontend',
    signal: 'ingress.requests',
    observed: 140 + residual,
    explained_base: 100,
    explained_event: 40,
    residual,
    band_low: 130,
    band_high: 150,
    residual_score: residual / 100,
    context_ids: [],
  };
}

function windowOf(frames: unknown[], extra: Record<string, unknown> = {}) {
  return {
    service: 'frontend',
    signal: 'ingress.requests',
    start: '2026-03-01T11:30:00Z',
    end: '2026-03-01T12:00:00Z',
    count: frames.length,
    truncated: false,
    limit: 1000,
    frames,
    ...extra,
  };
}

function serve(body: unknown, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status }))),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DecompositionPanel', () => {
  it('says an empty window is empty rather than drawing a flat line at zero', async () => {
    // The distinction the whole panel exists to keep: "nothing was recorded"
    // is not "a surge of zero". Drawing the second would invent a measurement
    // in the one place this product asks to be trusted.
    serve(windowOf([]));

    render(<DecompositionPanel />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/no frames recorded/i);
    });
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('draws the bands once there are frames', async () => {
    serve(windowOf([frame(0, 0), frame(2, 30)]));

    render(<DecompositionPanel />);

    await waitFor(() => {
      expect(screen.getByRole('img', { name: /decomposition of 2 frames/i })).toBeInTheDocument();
    });
    expect(screen.getByText('unexplained residual')).toBeInTheDocument();
  });

  it('tells the reader when the window was truncated', async () => {
    // A chart that hid this would draw a partial window as the whole story.
    serve(windowOf([frame(0, 0)], { truncated: true, limit: 1 }));

    render(<DecompositionPanel />);

    await waitFor(() => {
      expect(screen.getByText(/showing the first 1 frames/i)).toBeInTheDocument();
    });
  });

  it('distinguishes a missing store from a failed request', async () => {
    serve({ detail: 'no decomposition store is attached to this gateway', frames: [] }, 503);

    render(<DecompositionPanel />);

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/store is not attached/i);
    });
  });

  it('surfaces a frame whose own numbers do not add up', async () => {
    // The API sends the parts rather than a summary precisely so this is
    // checkable here; checking it is the difference between showing a
    // decomposition and asserting one.
    const broken = { ...frame(0, 10), observed: 999 };
    serve(windowOf([broken]));

    render(<DecompositionPanel />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/did not satisfy observed/i);
    });
  });
});
