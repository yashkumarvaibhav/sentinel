import { useId, useRef, type KeyboardEvent } from 'react';

import type { LucideIcon } from '@/ui/icons';

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: LucideIcon;
  /** Optional longer description for assistive tech, when the label is jargon. */
  description?: string;
}

interface SegmentedControlProps<T extends string> {
  /** Shown beside the control, not only announced. An unlabelled toggle between two jargon words is unreadable. */
  label: string;
  options: SegmentedOption<T>[];
  value: T;
  onChange: (next: T) => void;
  className?: string;
}

/**
 * A labelled choice where every option is visible at once.
 *
 * This replaces a single button whose caption was the *current* state, which
 * most people read as the state they are about to get — so the control appeared
 * to do nothing when in fact it did the opposite. Showing both options with one
 * of them marked removes the ambiguity entirely: there is nothing to infer.
 *
 * Implemented as a real `radiogroup` rather than a row of buttons, so it behaves
 * the way assistive technology expects a one-of-N choice to behave: the group is
 * one tab stop, arrow keys move within it, and the selected option is the one
 * that carries focus when you tab back in.
 */
export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  className = '',
}: SegmentedControlProps<T>) {
  const labelId = useId();
  const containerRef = useRef<HTMLDivElement>(null);

  function focusOption(index: number) {
    const wrapped = (index + options.length) % options.length;
    const option = options[wrapped];
    if (!option) return;
    onChange(option.value);
    const buttons = containerRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    buttons?.[wrapped]?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const current = options.findIndex((option) => option.value === value);
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault();
      focusOption(current + 1);
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault();
      focusOption(current - 1);
    }
  }

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <span id={labelId} className="eyebrow font-sans hidden sm:inline">
        {label}
      </span>
      <div
        ref={containerRef}
        role="radiogroup"
        aria-labelledby={labelId}
        onKeyDown={onKeyDown}
        className="border-line bg-raised inline-flex rounded-md border p-0.5"
      >
        {options.map((option) => {
          const selected = option.value === value;
          const Icon = option.icon;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={selected}
              // Roving tabindex: the group is one stop in the tab order and the
              // selected option is where focus lands, which is the whole reason
              // to use a radiogroup instead of three separate buttons.
              tabIndex={selected ? 0 : -1}
              aria-label={option.description}
              onClick={() => onChange(option.value)}
              className={`inline-flex min-h-11 items-center gap-1.5 rounded-sm px-2.5 text-xs font-bold transition-colors sm:min-h-0 sm:py-1.5 ${
                selected
                  ? 'bg-accent-soft text-accent-hover'
                  : 'text-muted hover:text-ink hover:bg-hover'
              }`}
            >
              {Icon && <Icon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />}
              {option.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
