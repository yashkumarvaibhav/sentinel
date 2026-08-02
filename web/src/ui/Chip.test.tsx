import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Chip, HonestyChip, type ChipTone } from '@/ui/Chip';

const TONES: ChipTone[] = ['healthy', 'degraded', 'offline', 'advisory', 'neutral'];

describe('Chip', () => {
  it.each(TONES)('pairs colour with an icon and a readable label (%s)', (tone) => {
    const { container } = render(<Chip tone={tone}>Ready</Chip>);

    // The house rule this enforces: status is never colour alone. A chip that
    // renders as a bare coloured pill fails here rather than in review.
    expect(container.querySelector('svg')).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
  });

  it('hides its icon from assistive technology, since the label already says it', () => {
    const { container } = render(<Chip tone="offline">Offline</Chip>);

    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });
});

describe('HonestyChip', () => {
  it.each(['REAL', 'SIMULATED', 'LIVE', 'REPLAY'] as const)(
    'renders %s as its own word, not as a colour',
    (kind) => {
      const { container } = render(<HonestyChip kind={kind} />);

      expect(screen.getByText(kind)).toBeInTheDocument();
      expect(container.querySelector('svg')).toBeInTheDocument();
    },
  );

  it('distinguishes replayed from live, which is the whole point of the label', () => {
    const { rerender, container } = render(<HonestyChip kind="LIVE" />);
    const live = container.querySelector('span')?.className;

    rerender(<HonestyChip kind="REPLAY" />);

    expect(container.querySelector('span')?.className).not.toBe(live);
  });
});
