import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { App } from '@/App';

describe('App', () => {
  it('renders the wordmark', () => {
    render(<App />);

    expect(screen.getByRole('heading', { level: 1, name: 'Sentinel' })).toBeInTheDocument();
  });

  it('labels illustrative content as simulated', () => {
    render(<App />);

    expect(screen.getByText('Simulated')).toBeInTheDocument();
  });
});
