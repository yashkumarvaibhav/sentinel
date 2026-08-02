import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { InfoPopover } from '@/ui/InfoPopover';

function renderPopover() {
  return render(
    <InfoPopover label="About the decomposition">
      An event explains volume, not behaviour.
    </InfoPopover>,
  );
}

describe('InfoPopover', () => {
  it('is a real button rather than a hover target or a title attribute', () => {
    renderPopover();

    // The distinction matters: `title=` never appears on touch and is not
    // announced by default, and a hover-only div is unreachable by keyboard.
    const trigger = screen.getByRole('button', { name: 'About the decomposition' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).not.toHaveAttribute('title');
  });

  it('opens from the keyboard alone', async () => {
    const user = userEvent.setup();
    renderPopover();

    await user.tab();
    expect(screen.getByRole('button', { name: 'About the decomposition' })).toHaveFocus();

    await user.keyboard('{Enter}');

    expect(screen.getByRole('note')).toHaveTextContent(/an event explains volume/i);
  });

  it('closes on Escape and puts focus back on the trigger', async () => {
    const user = userEvent.setup();
    renderPopover();
    const trigger = screen.getByRole('button', { name: 'About the decomposition' });

    await user.click(trigger);
    expect(screen.getByRole('note')).toBeInTheDocument();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    // Without this a keyboard user is dropped at the top of the document and
    // has to walk back to where they were.
    expect(trigger).toHaveFocus();
  });

  it('closes when the pointer goes elsewhere, without stealing focus back', async () => {
    const user = userEvent.setup();
    render(
      <>
        <InfoPopover label="About the decomposition">Prose.</InfoPopover>
        <button type="button">Somewhere else</button>
      </>,
    );

    await user.click(screen.getByRole('button', { name: 'About the decomposition' }));
    expect(screen.getByRole('note')).toBeInTheDocument();

    const elsewhere = screen.getByRole('button', { name: 'Somewhere else' });
    await user.click(elsewhere);

    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(elsewhere).toHaveFocus();
  });

  it('points the trigger at the panel only while the panel exists', async () => {
    const user = userEvent.setup();
    renderPopover();
    const trigger = screen.getByRole('button', { name: 'About the decomposition' });

    expect(trigger).not.toHaveAttribute('aria-controls');

    await user.click(trigger);

    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(trigger.getAttribute('aria-controls')).toBe(screen.getByRole('note').id);
  });
});
