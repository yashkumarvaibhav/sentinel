import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ConnectionBanner } from '@/shell/ConnectionBanner';
import type { LivenessState } from '@/shell/useLiveness';

const state = vi.hoisted((): { enabled: boolean; liveness: LivenessState } => ({
  enabled: true,
  liveness: {
    status: 'live',
    report: null,
    lastContact: null,
    transport: 'sse',
    detail: null,
  },
}));
const playHooter = vi.hoisted(() => vi.fn());

vi.mock('@/shell/hooter', () => ({ playHooter }));
vi.mock('@/shell/useSound', () => ({
  useSound: () => ({ enabled: state.enabled, setEnabled: vi.fn() }),
}));
vi.mock('@/shell/useLiveness', () => ({ useLiveness: () => state.liveness }));

beforeEach(() => {
  state.enabled = true;
  state.liveness = {
    status: 'live',
    report: null,
    lastContact: null,
    transport: 'sse',
    detail: null,
  };
  playHooter.mockClear();
});

describe('ConnectionBanner', () => {
  it('interrupts and sounds when a healthy platform becomes degraded', () => {
    const view = render(<ConnectionBanner />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    state.liveness = {
      ...state.liveness,
      status: 'degraded',
      report: {
        status: 'degraded',
        components: [],
        degraded: ['postgres'],
      },
    };
    view.rerender(<ConnectionBanner />);

    expect(screen.getByRole('alert')).toHaveTextContent(/platform degraded.*postgres/i);
    expect(playHooter).toHaveBeenCalledTimes(1);
  });

  it('keeps an existing failure visible without blasting on page load', () => {
    state.liveness = { ...state.liveness, status: 'down', detail: 'gateway unavailable' };
    render(<ConnectionBanner />);

    expect(screen.getByRole('alert')).toHaveTextContent(/live stream disconnected/i);
    expect(playHooter).not.toHaveBeenCalled();
  });
});
