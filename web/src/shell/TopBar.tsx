import { useEffect, useState, type RefObject } from 'react';
import { Link } from 'react-router';

import { fetchVersion } from '@/api/platform';
import type { VersionInfo } from '@/api/platform';
import { useLiveness } from '@/shell/useLiveness';
import type { Liveness, LivenessState } from '@/shell/useLiveness';
import { SearchPalette } from '@/shell/SearchPalette';
import { SoundToggle } from '@/shell/SoundToggle';
import { ThemeToggle } from '@/shell/ThemeToggle';
import { BrandMark } from '@/ui/BrandMark';
import { BUTTON_BASE, BUTTON_VARIANT } from '@/ui/buttonStyles';
import { Chip, type ChipTone } from '@/ui/Chip';
import { Activity, LoaderCircle, TriangleAlert, WifiOff, Zap, type LucideIcon } from '@/ui/icons';

/**
 * Each connection state is a tone *and* an icon *and* the word itself. The
 * previous version was a coloured dot with a screen-reader-only sentence, which
 * left a sighted user who cannot separate the greens and ambers with nothing to
 * read — the exact "colour alone" failure the house rules name as the most
 * violated one in UI work.
 */
const DOT: Record<Liveness, { tone: ChipTone; icon: LucideIcon; label: string }> = {
  connecting: { tone: 'neutral', icon: LoaderCircle, label: 'Connecting to the platform' },
  live: { tone: 'healthy', icon: Activity, label: 'Live stream open, every component ready' },
  degraded: {
    tone: 'degraded',
    icon: TriangleAlert,
    label: 'Live stream open, some component degraded',
  },
  reconnecting: {
    tone: 'degraded',
    icon: LoaderCircle,
    label: 'Live stream disconnected; reconnecting automatically',
  },
  down: { tone: 'offline', icon: WifiOff, label: 'Live stream unavailable' },
};

function ConnectionDot({ liveness }: { liveness: LivenessState }) {
  const dot = DOT[liveness.status];
  const degraded = liveness.report?.degraded ?? [];
  const healthDetail =
    liveness.status === 'degraded' && liveness.detail !== null && degraded.length === 0
      ? 'Live stream open, component health snapshot unavailable'
      : liveness.status === 'degraded' && degraded.length > 0
        ? `${dot.label}: ${degraded.join(', ')}`
        : dot.label;
  const detail = liveness.detail ? `${healthDetail}: ${liveness.detail}` : healthDetail;

  return (
    <span
      className="flex items-center gap-2"
      title={`${detail} — SSE live; snapshots refetch after reconnect`}
    >
      {/* The full sentence is announced; the chip beside it carries the same
          fact visually as colour + icon + the state's own name. */}
      <span className="sr-only" role="status">
        {detail}
      </span>
      <Chip tone={dot.tone} icon={dot.icon}>
        {liveness.status}
      </Chip>
    </span>
  );
}

function useBuildStamp(): VersionInfo | null {
  const [version, setVersion] = useState<VersionInfo | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchVersion(controller.signal)
      .then(setVersion)
      .catch(() => {
        // A missing build stamp is not worth a visible error: the connection
        // dot is already saying the gateway is unreachable.
      });
    return () => {
      controller.abort();
    };
  }, []);

  return version;
}

/**
 * The persistent command shell header (`UIUX_SPEC` §3).
 *
 * The build stamp is here rather than in a footer because it is the answer to
 * "is what I am looking at the commit I just pushed?", and that question is
 * asked while looking at the top of the page.
 */
export function TopBar({
  onOpenNav,
  navOpen = false,
  openNavRef,
}: {
  onOpenNav?: () => void;
  navOpen?: boolean;
  openNavRef?: RefObject<HTMLButtonElement | null>;
} = {}) {
  const liveness = useLiveness();
  const version = useBuildStamp();

  return (
    <header className="border-line bg-page sticky top-0 z-30 border-b">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 sm:gap-x-4 sm:px-6 lg:px-8">
        {onOpenNav && (
          <button
            ref={openNavRef}
            type="button"
            onClick={onOpenNav}
            aria-label="Open navigation"
            aria-expanded={navOpen}
            className="border-line text-ink hover:bg-hover flex size-11 shrink-0 items-center justify-center rounded-md border lg:hidden"
          >
            <svg
              aria-hidden="true"
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            >
              <path d="M4 6.5h16M4 12h16M4 17.5h16" />
            </svg>
          </button>
        )}

        {/* The wordmark rides in the rail on desktop; here it is the mobile
            fallback, at the same proportions the family ships everywhere. */}
        <a href="/command" className="flex min-w-0 items-center gap-2.5 lg:hidden">
          <span className="border-line bg-raised flex size-10 shrink-0 items-center justify-center rounded-sm border">
            <BrandMark className="size-8" />
          </span>
          <span className="text-ink hidden truncate font-serif text-2xl font-medium tracking-tight sm:inline">
            Sentinel
          </span>
        </a>

        <SearchPalette />

        <ConnectionDot liveness={liveness} />

        <div className="ml-auto flex items-center gap-2 sm:gap-3">
          {/* The one teal-filled control on the screen. Teal means "you can
              act here", and until now the product's own signal for that was
              entirely unused — every control was a ghost button. Firing a
              scenario is the action this console is for. */}
          <Link to="/demo" className={`${BUTTON_BASE} ${BUTTON_VARIANT.primary} shrink-0`}>
            <Zap aria-hidden="true" className="size-4 shrink-0" strokeWidth={2} />
            <span className="hidden sm:inline">Fire a scenario</span>
            <span className="sm:hidden">Fire</span>
          </Link>

          <SoundToggle />

          <ThemeToggle />

          {version && (
            <code
              className="text-muted hidden font-mono text-[11px] md:inline"
              title={`${version.service} ${version.version}, built ${version.built_at} (${version.env})`}
            >
              {version.short_sha}
            </code>
          )}
        </div>
      </div>
    </header>
  );
}
