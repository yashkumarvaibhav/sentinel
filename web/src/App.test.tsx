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

  it('labels illustrative content as simulated', () => {
    render(<App />);

    expect(screen.getByText('Simulated')).toBeInTheDocument();
  });

  it('offers a skip link ahead of the header', () => {
    render(<App />);

    expect(screen.getByRole('link', { name: 'Skip to content' })).toBeInTheDocument();
  });
});
