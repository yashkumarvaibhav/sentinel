export type ButtonVariant = 'primary' | 'ghost' | 'danger';

/**
 * Three variants, and only three (`~/brand-kit/patterns.md`).
 *
 * Teal fill is the *only* primary. The house rule the previous UI broke in both
 * directions is that teal means "you can act here" while green/amber/red mean
 * "this is the state" — so there is never a green confirm button, and a
 * destructive action is red *text*, not a red fill. A red fill reads as an alarm
 * the operator is being shown, not as a control they are being offered.
 *
 * These live apart from the component so a `Link` that must look like a button
 * can wear the same classes without the two drifting. A navigation action is a
 * link — middle-click and "open in new tab" have to keep working.
 */
export const BUTTON_VARIANT: Record<ButtonVariant, string> = {
  primary: 'bg-accent text-accent-contrast border-transparent hover:bg-accent-hover',
  ghost: 'bg-raised text-ink border-line hover:bg-hover hover:border-line-strong',
  danger: 'bg-raised text-danger border-line hover:bg-danger-soft',
};

export const BUTTON_BASE =
  'inline-flex min-h-11 items-center justify-center gap-2 rounded-md border px-3 text-xs font-bold transition-colors sm:min-h-0 sm:py-2';
