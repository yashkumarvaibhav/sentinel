import type { ReactNode } from 'react';

import {
  CircleCheck,
  CircleDashed,
  CircleX,
  FlaskConical,
  History,
  Info,
  RadioTower,
  ShieldCheck,
  TriangleAlert,
  type LucideIcon,
} from '@/ui/icons';

export type ChipTone = 'healthy' | 'degraded' | 'offline' | 'advisory' | 'neutral';

/**
 * Colour is the third signal a status carries, never the first and never the
 * only one (`~/brand-kit/brand-guidelines.md` §7). Each tone therefore ships a
 * default icon, and the label is a required child — a chip that renders as a
 * bare coloured pill is not constructible from this component.
 */
const TONE: Record<ChipTone, { className: string; icon: LucideIcon }> = {
  healthy: { className: 'text-success border-success-line', icon: CircleCheck },
  degraded: { className: 'text-warning border-warning-line', icon: TriangleAlert },
  offline: { className: 'text-danger border-danger-line', icon: CircleX },
  advisory: { className: 'text-info border-info-line', icon: Info },
  neutral: { className: 'text-muted border-line', icon: CircleDashed },
};

interface ChipProps {
  tone: ChipTone;
  /** Overrides the tone's default icon; it never removes it. */
  icon?: LucideIcon;
  children: ReactNode;
  className?: string;
  title?: string;
}

export function Chip({ tone, icon, children, className = '', title }: ChipProps) {
  const { className: toneClass, icon: ToneIcon } = TONE[tone];
  const Icon = icon ?? ToneIcon;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border bg-transparent px-2.5 py-1 text-[0.72rem] font-bold whitespace-nowrap ${toneClass} ${className}`}
      title={title}
    >
      <Icon aria-hidden="true" className="size-3 shrink-0" strokeWidth={2.25} />
      {children}
    </span>
  );
}

export type HonestyKind = 'REAL' | 'SIMULATED' | 'LIVE' | 'REPLAY';

/**
 * The honesty labels (`UIUX_SPEC.md` §1.3), which are UI rather than footnotes.
 *
 * `REAL` and `LIVE` are the only two that may read as an assurance, so they are
 * the only two that carry a colour other than the neutral grey — and they are
 * rendered only where the caller genuinely has the real or live thing. The
 * remediation brief lists these chips as load-bearing: they stay visible on
 * every surface that carries them today.
 */
const HONESTY: Record<HonestyKind, { className: string; icon: LucideIcon; title: string }> = {
  REAL: {
    className: 'text-success border-success-line',
    icon: ShieldCheck,
    title: 'Measured from real testbed telemetry',
  },
  SIMULATED: {
    className: 'text-muted border-line',
    icon: FlaskConical,
    title: 'Produced by a scripted or replayed scenario, not measured live',
  },
  LIVE: {
    className: 'text-success border-success-line',
    icon: RadioTower,
    title: 'Running now on the live testbed',
  },
  REPLAY: {
    className: 'text-muted border-line',
    icon: History,
    title: 'Replayed from a recorded capture — bit-exact, not happening now',
  },
};

export function HonestyChip({ kind, className = '' }: { kind: HonestyKind; className?: string }) {
  const { className: kindClass, icon: Icon, title } = HONESTY[kind];

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border bg-transparent px-2.5 py-1 text-[0.72rem] font-bold tracking-wider whitespace-nowrap ${kindClass} ${className}`}
      title={title}
    >
      <Icon aria-hidden="true" className="size-3 shrink-0" strokeWidth={2.25} />
      {kind}
    </span>
  );
}
