import type { ButtonHTMLAttributes, ReactNode } from 'react';

import type { LucideIcon } from '@/ui/icons';

export type ButtonVariant = 'primary' | 'ghost' | 'danger';

/**
 * Three variants, and only three (`~/brand-kit/patterns.md`).
 *
 * Teal fill is the *only* primary. The house rule the previous UI broke in both
 * directions is that teal means "you can act here" while green/amber/red mean
 * "this is the state" — so there is never a green confirm button, and a
 * destructive action is red *text*, not a red fill. A red fill reads as an alarm
 * the operator is being shown, not as a control they are being offered.
 */
const VARIANT: Record<ButtonVariant, string> = {
  primary: 'bg-accent text-accent-contrast border-transparent hover:bg-accent-hover',
  ghost: 'bg-raised text-ink border-line hover:bg-hover hover:border-line-strong',
  danger: 'bg-raised text-danger border-line hover:bg-danger-soft',
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** Rendered before the label and hidden from assistive tech — the label carries the meaning. */
  icon?: LucideIcon;
  children?: ReactNode;
}

export function Button({
  variant = 'ghost',
  icon: Icon,
  children,
  className = '',
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-md border px-3 text-xs font-bold transition-colors sm:min-h-0 sm:py-2 ${VARIANT[variant]} disabled:cursor-not-allowed disabled:opacity-55 ${className}`}
      {...rest}
    >
      {Icon && <Icon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />}
      {children}
    </button>
  );
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: LucideIcon;
  /** Required: an icon-only control is invisible to a screen reader without it. */
  label: string;
  variant?: ButtonVariant;
}

/**
 * An icon-only control. Fixed at 44x44 in every density — the kit's touch-target
 * floor is about fingers, and a finger does not get smaller on a wide viewport.
 */
export function IconButton({
  icon: Icon,
  label,
  variant = 'ghost',
  className = '',
  type = 'button',
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      aria-label={label}
      className={`grid size-11 shrink-0 place-items-center rounded-md border transition-colors ${VARIANT[variant]} ${className}`}
      {...rest}
    >
      <Icon aria-hidden="true" className="size-[18px]" strokeWidth={2} />
    </button>
  );
}
