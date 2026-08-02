import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '@/App';
import { CommandShell } from '@/shell/CommandShell';

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
  function renderApp() {
    return render(
      <MemoryRouter initialEntries={['/command']}>
        <Routes>
          <Route element={<CommandShell />}>
            <Route path="/command" element={<App />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
  }

  it('names the screen, not the product, in its heading', () => {
    renderApp();

    // The wordmark moved into the shell header when it became persistent. A
    // page whose <h1> is the product name tells a screen-reader user which
    // site they are on and nothing about where they are in it.
    expect(
      screen.getByRole('heading', { level: 1, name: 'Command center' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sentinel' })).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Command' })).toHaveAttribute('href', '/command');
    expect(screen.getByRole('link', { name: 'Incidents' })).toHaveAttribute(
      'href',
      '/command#live-incidents',
    );
    expect(screen.getByRole('link', { name: 'Security' })).toHaveAttribute('href', '/security');
  });

  it('labels the decomposition as real, because it now reads the real store', () => {
    renderApp();

    // This asserted `Simulated` until the hero chart replaced the illustrative
    // sketch. The label flipped because the data source did - it is a claim
    // about where the numbers come from, and it must never move ahead of them.
    //
    // The casing moved from `Real` to `REAL` when the honesty labels became one
    // shared chip: this surface used title case while the score proof beside it
    // used caps, for the same claim. The claim itself is unchanged, and the
    // second assertion is the one carrying it - real is asserted, simulated is
    // still refused.
    expect(screen.getAllByText('REAL').length).toBeGreaterThan(0);
    expect(screen.queryByText('SIMULATED')).not.toBeInTheDocument();
  });

  it('offers a skip link ahead of the header', () => {
    renderApp();

    expect(screen.getByRole('link', { name: 'Skip to content' })).toBeInTheDocument();
  });

  it('shares one event stream across health and incident snapshot owners', () => {
    CountingEventSource.instances = [];
    vi.stubGlobal('EventSource', CountingEventSource);

    const rendered = renderApp();

    expect(CountingEventSource.instances).toHaveLength(1);
    rendered.unmount();
    expect(CountingEventSource.instances[0]?.readyState).toBe(2);
  });
});
