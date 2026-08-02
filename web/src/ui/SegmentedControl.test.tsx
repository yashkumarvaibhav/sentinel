import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import { SegmentedControl } from '@/ui/SegmentedControl';

type Mode = 'a' | 'b' | 'c';

function Harness({ initial = 'a' }: { initial?: Mode }) {
  const [value, setValue] = useState<Mode>(initial);
  return (
    <SegmentedControl<Mode>
      label="Mode"
      value={value}
      onChange={setValue}
      options={[
        { value: 'a', label: 'Alpha', description: 'Alpha mode' },
        { value: 'b', label: 'Beta', description: 'Beta mode' },
        { value: 'c', label: 'Gamma', description: 'Gamma mode' },
      ]}
    />
  );
}

describe('SegmentedControl', () => {
  it('shows every option at once and marks the selected one', () => {
    render(<Harness />);

    expect(screen.getByRole('radiogroup', { name: 'Mode' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Alpha mode' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('radio', { name: 'Beta mode' })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('is one tab stop, entered at the selected option', async () => {
    const user = userEvent.setup();
    render(<Harness initial="b" />);

    await user.tab();

    // The roving tabindex is the reason to use a radiogroup rather than three
    // buttons: tabbing through a toolbar should not mean walking every option.
    expect(screen.getByRole('radio', { name: 'Beta mode' })).toHaveFocus();
    expect(screen.getByRole('radio', { name: 'Alpha mode' })).toHaveAttribute('tabindex', '-1');
  });

  it('moves the selection with the arrow keys', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.tab();
    await user.keyboard('{ArrowRight}');

    expect(screen.getByRole('radio', { name: 'Beta mode' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('radio', { name: 'Beta mode' })).toHaveFocus();
  });

  it('wraps around rather than dead-ending at either edge', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.tab();
    await user.keyboard('{ArrowLeft}');

    expect(screen.getByRole('radio', { name: 'Gamma mode' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
  });
});
