import { useAudience, type Audience } from '@/shell/useAudience';
import { Button } from '@/ui/Button';
import { ArrowLeftRight, Briefcase, Terminal, type LucideIcon } from '@/ui/icons';

const VIEW: Record<
  Audience,
  { name: string; controls: string; icon: LucideIcon; offers: string; other: Audience }
> = {
  technical: {
    name: 'Technical view',
    controls: 'Scores, evidence values against baseline, residuals, provenance and raw telemetry.',
    icon: Terminal,
    offers: 'Switch to executive view',
    other: 'exec',
  },
  exec: {
    name: 'Executive view',
    controls: 'Plain language and decision-critical facts only. The same data, with the machinery folded away.',
    icon: Briefcase,
    offers: 'Switch to technical view',
    other: 'technical',
  },
};

/**
 * Which audience the console is speaking to, and one button offering the other.
 *
 * Three shapes have now been tried here. A single button captioned with the
 * audience you were already in read as "click for this" — the opposite of what
 * it did. A segmented control fixed the ambiguity but said nothing about what
 * either option *means*, which is no help to the executive it exists for. This
 * is the pattern the family already uses for exactly this problem: name the
 * mode you are in, say in one line what it controls, and offer the other by
 * name. Nothing has to be inferred and nothing is jargon standing alone.
 *
 * It also gives the header something to be about. The wordmark moved to the
 * rail, and what was left was a live chip and a row of icon buttons.
 *
 * `useAudience` is untouched: Exec and Technical are two renderings of one
 * dataset (`UIUX_SPEC.md` §1.4), and no component fetches differently for them.
 */
export function AudienceBar() {
  const { audience, setAudience } = useAudience();
  const view = VIEW[audience];
  const Icon = view.icon;

  return (
    <section
      aria-label="Active view"
      className="border-line bg-accent-soft border-b px-4 py-2.5 sm:px-6 lg:px-8"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <span className="bg-raised text-accent flex size-9 shrink-0 items-center justify-center rounded-md">
            <Icon aria-hidden="true" className="size-4" strokeWidth={2} />
          </span>
          <div className="min-w-0">
            <p className="text-ink font-serif text-base">{view.name}</p>
            <p className="text-muted truncate text-xs">{view.controls}</p>
          </div>
        </div>

        <Button
          onClick={() => setAudience(view.other)}
          icon={ArrowLeftRight}
          className="shrink-0 whitespace-nowrap"
        >
          {view.offers}
        </Button>
      </div>
    </section>
  );
}
