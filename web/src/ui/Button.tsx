import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { BUTTON_BASE, BUTTON_VARIANT, type ButtonVariant } from '@/ui/buttonStyles';
import type { LucideIcon } from '@/ui/icons';


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
      className={`${BUTTON_BASE} ${BUTTON_VARIANT[variant]} disabled:cursor-not-allowed disabled:opacity-55 ${className}`}
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
      className={`grid size-11 shrink-0 place-items-center rounded-md border transition-colors ${BUTTON_VARIANT[variant]} ${className}`}
      {...rest}
    >
      <Icon aria-hidden="true" className="size-[18px]" strokeWidth={2} />
    </button>
  );
}
