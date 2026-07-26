import { useEffect, useState } from 'react';

import { fetchVersion } from '@/api/platform';
import type { VersionInfo } from '@/api/platform';
import { useAudience } from '@/shell/useAudience';
import { useLiveness } from '@/shell/useLiveness';
import type { Liveness, LivenessState } from '@/shell/useLiveness';
import { useTheme } from '@/shell/useTheme';

const DOT: Record<Liveness, { className: string; label: string }> = {
  connecting: { className: 'bg-muted', label: 'Connecting to the platform' },
  live: { className: 'bg-ok', label: 'Live stream open, every component ready' },
  degraded: { className: 'bg-warn', label: 'Live stream open, some component degraded' },
  reconnecting: {
    className: 'bg-warn',
    label: 'Live stream disconnected; reconnecting automatically',
  },
  down: { className: 'bg-bad', label: 'Live stream unavailable' },
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
      <span className={`inline-block size-2 rounded-full ${dot.className}`} aria-hidden="true" />
      {/* The status is announced, not merely coloured: a dot is invisible to a
          screen reader and indistinguishable to a good share of sighted users. */}
      <span className="sr-only" role="status">
        {detail}
      </span>
      <span className="text-muted hidden text-xs sm:inline">{liveness.status}</span>
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
          <button
            type="button"
            onClick={toggleAudience}
            aria-pressed={audience === 'exec'}
            className="border-line hover:bg-accent-soft min-h-11 rounded-md border px-3 text-xs font-medium sm:min-h-0 sm:py-1.5"
          >
            {audience === 'exec' ? 'Exec' : 'Technical'}
          </button>

          <button
            type="button"
            onClick={cycleTheme}
            className="border-line hover:bg-accent-soft min-h-11 rounded-md border px-3 text-xs font-medium sm:min-h-0 sm:py-1.5"
            aria-label={`Theme: ${preference}. Activate to change.`}
          >
            {preference === 'system' ? 'Auto' : preference === 'light' ? 'Light' : 'Dark'}
          </button>

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
