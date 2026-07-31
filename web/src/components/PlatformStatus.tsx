import { useEffect, useState } from 'react';

import { fetchHealth, fetchVersion } from '@/api/platform';
import type { HealthReport, VersionInfo } from '@/api/platform';

type Load<T> = { state: 'loading' } | { state: 'ok'; data: T } | { state: 'error'; error: string };

function useMeta() {
  const [health, setHealth] = useState<Load<HealthReport>>({ state: 'loading' });
  const [version, setVersion] = useState<Load<VersionInfo>>({ state: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    fetchHealth(signal)
      .then((data) => setHealth({ state: 'ok', data }))
      .catch((error: unknown) => {
        if (!signal.aborted) setHealth({ state: 'error', error: describe(error) });
      });

    fetchVersion(signal)
      .then((data) => setVersion({ state: 'ok', data }))
      .catch((error: unknown) => {
        if (!signal.aborted) setVersion({ state: 'error', error: describe(error) });
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

function Dot({ ready }: { ready: boolean }) {
  return (
    <span
      className={`inline-block size-2 rounded-full ${ready ? 'bg-decomp-base' : 'bg-residual'}`}
      aria-hidden="true"
    />
  );
}

/** Which parts of our own pipeline are up, and which commit is serving. */
export function PlatformStatus() {
  const { health, version } = useMeta();

  return (
    <section className="bg-raised flex flex-col gap-4 rounded-xl p-5">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-medium tracking-wide uppercase">Platform</h2>
        <span className="text-muted text-[11px] tracking-wider uppercase">Real</span>
      </div>

      {health.state === 'loading' && <p className="text-muted text-sm">Checking…</p>}

      {health.state === 'error' && (
        <p className="text-sm">
          <span className="text-residual">Gateway unreachable</span>
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
                <Dot ready={component.ready} />
                <span className={component.ready ? '' : 'text-residual'}>{component.name}</span>
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
