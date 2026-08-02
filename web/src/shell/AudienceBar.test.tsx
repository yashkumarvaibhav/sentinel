import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { AudienceBar } from '@/shell/AudienceBar';

beforeEach(() => {
  window.localStorage.clear();
});

describe('AudienceBar', () => {
  it('names the view you are in and offers the other by name', async () => {
    const user = userEvent.setup();
    render(<AudienceBar />);

    // Technical is the default: the platform's claim is that every verdict is
    // legible from its evidence, so hiding the evidence by default would argue
    // against the product.
    expect(screen.getByText('Technical view')).toBeInTheDocument();

    // The control that used to be here was captioned with the audience you were
    // already in, which most people read as the one they were about to get.
    const offer = screen.getByRole('button', { name: /switch to executive view/i });
    await user.click(offer);

    expect(screen.getByText('Executive view')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /switch to technical view/i }),
    ).toBeInTheDocument();
  });

  it('says what the active view actually controls, not just its name', () => {
    render(<AudienceBar />);

    // Two jargon words alone are no help to the executive this exists for.
    expect(screen.getByText(/evidence values against baseline/i)).toBeInTheDocument();
  });

  it('persists the choice, so a reload does not silently change what is shown', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<AudienceBar />);

    await user.click(screen.getByRole('button', { name: /switch to executive view/i }));
    unmount();
    render(<AudienceBar />);

    expect(screen.getByText('Executive view')).toBeInTheDocument();
  });
});
