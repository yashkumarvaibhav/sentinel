import { useEffect, useState } from 'react';

import { fetchVersion } from '@/api/platform';
import type { VersionInfo } from '@/api/platform';
import { useAudience } from '@/shell/useAudience';
import { useLiveness } from '@/shell/useLiveness';
import type { Liveness, LivenessState } from '@/shell/useLiveness';
import { useTheme } from '@/shell/useTheme';
import { Button } from '@/ui/Button';
import { Chip, type ChipTone } from '@/ui/Chip';
import {
  Activity,
  Briefcase,
  LoaderCircle,
  Monitor,
  Moon,
  Sun,
  Terminal,
  TriangleAlert,
  WifiOff,
  type LucideIcon,
} from '@/ui/icons';

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
export function TopBar() {
  const liveness = useLiveness();
  const version = useBuildStamp();
  const { audience, toggle: toggleAudience } = useAudience();
  const { preference, cycle: cycleTheme } = useTheme();

  return (
    <header className="border-line bg-raised sticky top-0 z-10 border-b">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 sm:px-6">
        <a href="/command" className="text-base font-semibold tracking-tight">
          Sentinel
        </a>

        <ConnectionDot liveness={liveness} />

        <div className="ml-auto flex items-center gap-2">
          {/* Both controls still report the state they are IN rather than the
              action they offer — the inversion the house patterns warn about.
              That is 6.11c's slice; this one only puts them on the shared
              control vocabulary, so the fix lands in one place rather than in
              two more ad-hoc class strings. */}
          <Button
            onClick={toggleAudience}
            aria-pressed={audience === 'exec'}
            icon={audience === 'exec' ? Briefcase : Terminal}
          >
            {audience === 'exec' ? 'Exec' : 'Technical'}
          </Button>

          <Button
            onClick={cycleTheme}
            aria-label={`Theme: ${preference}. Activate to change.`}
            icon={preference === 'system' ? Monitor : preference === 'light' ? Sun : Moon}
          >
            {preference === 'system' ? 'Auto' : preference === 'light' ? 'Light' : 'Dark'}
          </Button>

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
