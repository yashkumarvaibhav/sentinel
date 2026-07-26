import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { App } from '@/App';

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
});
