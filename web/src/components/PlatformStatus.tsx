import { useCallback, useEffect, useState } from 'react';

import { fetchHealth, fetchVersion } from '@/api/platform';
import type { HealthReport, VersionInfo } from '@/api/platform';
import { HonestyChip } from '@/ui/Chip';
import { SkeletonText } from '@/ui/Skeleton';
import { CircleCheck, CircleX } from '@/ui/icons';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

type Load<T> = { state: 'loading' } | { state: 'ok'; data: T } | { state: 'error'; error: string };

function useMeta() {
  const [health, setHealth] = useState<Load<HealthReport>>({ state: 'loading' });
  const [version, setVersion] = useState<Load<VersionInfo>>({ state: 'loading' });

  const readHealth = useCallback((): Promise<void> => {
    const controller = new AbortController();
    return fetchHealth(controller.signal)
      .then((data) => setHealth({ state: 'ok', data }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setHealth({ state: 'error', error: describe(error) });
      });
  }, []);

  useSnapshotInvalidation(['health'], readHealth);

  useEffect(() => {
    void readHealth();
    const timer = window.setInterval(() => void readHealth(), 10_000);
    return () => window.clearInterval(timer);
  }, [readHealth]);

  useEffect(() => {
    const controller = new AbortController();

    fetchVersion(controller.signal)
      .then((data) => setVersion({ state: 'ok', data }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setVersion({ state: 'error', error: describe(error) });
      });

    return () => {
      controller.abort();
    };
  }, []);

  return { health, version };
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * A component's readiness, as shape first and colour second.
 *
 * This was a coloured dot drawing on `--color-decomp-base` / `--color-residual`
 * — the decomposition's own tokens. Those mean "this part of the surge was
 * explained" and "this part was not"; they are deliberately theme-independent
 * so a screenshot of a residual means one fixed thing, and spending them on
 * platform health blurs a domain claim into a status readout. Health belongs to
 * the semantic set, which is what `--success` and `--danger` are for.
 */
function ReadyMark({ ready }: { ready: boolean }) {
  const Icon = ready ? CircleCheck : CircleX;
  return (
    <>
      <Icon
        aria-hidden="true"
        className={`size-3.5 shrink-0 ${ready ? 'text-success' : 'text-danger'}`}
        strokeWidth={2.25}
      />
      <span className="sr-only">{ready ? 'ready' : 'not ready'}</span>
    </>
  );
}

/** Which parts of our own pipeline are up, and which commit is serving. */
export function PlatformStatus({ className = '' }: { className?: string } = {}) {
  const { health, version } = useMeta();

  return (
    <section
      className={`border-line bg-raised flex min-w-0 flex-col gap-4 rounded-lg border p-5 ${className}`}
    >
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="font-serif text-base">Platform</h2>
        <HonestyChip kind="REAL" />
      </div>

      {health.state === 'loading' && (
        <SkeletonText lines={4} label="Checking every plane…" />
      )}

      {health.state === 'error' && (
        <p className="text-sm">
          <span className="text-danger">Gateway unreachable</span>
          <span className="text-muted"> — {health.error}</span>
        </p>
      )}

      {health.state === 'ok' && (
        <>
          <p className="text-sm">
            {health.data.status === 'ready'
              ? 'All planes ready.'
              : `Degraded: ${health.data.degraded.join(', ')}`}
          </p>
          <ul className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
            {health.data.components.map((component) => (
              <li key={component.name} className="flex items-center gap-2">
                <ReadyMark ready={component.ready} />
                <span className={component.ready ? '' : 'text-danger'}>{component.name}</span>
                <span className="text-muted ml-auto tabular-nums">
                  {Math.round(component.latency_ms)} ms
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      <p className="text-muted border-line border-t pt-3 text-xs">
        {version.state === 'ok'
          ? `${version.data.service} ${version.data.version} · ${version.data.short_sha} · built ${version.data.built_at} · ${version.data.env}`
          : 'build stamp unavailable'}
      </p>
    </section>
  );
}
