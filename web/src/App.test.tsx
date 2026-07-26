import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '@/App';

class CountingEventSource {
  static instances: CountingEventSource[] = [];

  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string | URL) {
    CountingEventSource.instances.push(this);
  }

  addEventListener(): void {}
  removeEventListener(): void {}

  close(): void {
    this.readyState = 2;
  }
}

beforeEach(() => {
  // App structure tests do not own any remote snapshot. Keep those reads
  // pending so a rejected test-network promise cannot update four unrelated
  // components after the synchronous assertion has already finished.
  vi.stubGlobal('fetch', () => new Promise<Response>(() => undefined));
});

afterEach(() => vi.unstubAllGlobals());

describe('App', () => {
  it('names the screen, not the product, in its heading', () => {
    render(<App />);

    // The wordmark moved into the shell header when it became persistent. A
    // page whose <h1> is the product name tells a screen-reader user which
    // site they are on and nothing about where they are in it.
    expect(
      screen.getByRole('heading', { level: 1, name: 'Command center' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sentinel' })).toBeInTheDocument();
  });

  it('labels the decomposition as real, because it now reads the real store', () => {
    render(<App />);

    // This asserted `Simulated` until the hero chart replaced the illustrative
    // sketch. The label flipped because the data source did - it is a claim
    // about where the numbers come from, and it must never move ahead of them.
    expect(screen.getAllByText('Real').length).toBeGreaterThan(0);
    expect(screen.queryByText('Simulated')).not.toBeInTheDocument();
  });

  it('offers a skip link ahead of the header', () => {
    render(<App />);

    expect(screen.getByRole('link', { name: 'Skip to content' })).toBeInTheDocument();
  });

  it('shares one event stream across health and incident snapshot owners', () => {
    CountingEventSource.instances = [];
    vi.stubGlobal('EventSource', CountingEventSource);

    const rendered = render(<App />);

    expect(CountingEventSource.instances).toHaveLength(1);
    rendered.unmount();
    expect(CountingEventSource.instances[0]?.readyState).toBe(2);
  });
});
