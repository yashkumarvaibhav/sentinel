import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RelativeTime } from '@/ui/RelativeTime';

const NOW = new Date('2026-08-02T12:00:00Z');

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function iso(secondsAgo: number): string {
  return new Date(NOW.getTime() - secondsAgo * 1000).toISOString();
}

describe('RelativeTime', () => {
  it('returns a relative time, which is what the function it replaces did not', () => {
    // `timeAgo()` was named for a relative time and returned an absolute UTC
    // stamp, so nothing on a live page ever visibly changed.
    render(<RelativeTime iso={iso(420)} />);

    expect(screen.getByText('7 min ago')).toBeInTheDocument();
  });

  it('keeps the absolute stamp reachable rather than throwing it away', () => {
    render(<RelativeTime iso={iso(420)} />);

    // "7 min ago" is right for a glance and wrong for an incident report.
    const element = screen.getByText('7 min ago');
    expect(element).toHaveAttribute('dateTime', iso(420));
    expect(element.getAttribute('title')).toMatch(/UTC$/);
  });

  it('ticks without a reload', () => {
    render(<RelativeTime iso={iso(5)} />);
    expect(screen.getByText('just now')).toBeInTheDocument();

    // The interval drives a state update, so the clock has to move inside act.
    act(() => {
      vi.advanceTimersByTime(30_000);
    });

    expect(screen.getByText('35s ago')).toBeInTheDocument();
  });

  it('does not render a future timestamp as a negative age', () => {
    render(<RelativeTime iso={iso(-90)} />);

    expect(screen.getByText('just now')).toBeInTheDocument();
  });
});
