/** Shapes served by the gateway's meta endpoints. */

export interface ComponentHealth {
  name: string;
  ready: boolean;
  latency_ms: number;
  detail: string | null;
}

export interface HealthReport {
  status: 'ready' | 'degraded';
  degraded: string[];
  components: ComponentHealth[];
}

export interface VersionInfo {
  service: string;
  version: string;
  git_sha: string;
  short_sha: string;
  built_at: string;
  env: string;
}

/**
 * The gateway answers 503 with a full body when a dependency is down — that is
 * a real answer about the platform's state, so it is parsed like any other.
 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthReport> {
  const response = await fetch('/api/health', signal ? { signal } : {});
  if (response.status !== 200 && response.status !== 503) {
    throw new Error(`health request failed: ${response.status}`);
  }
  return (await response.json()) as HealthReport;
}

export async function fetchVersion(signal?: AbortSignal): Promise<VersionInfo> {
  const response = await fetch('/api/version', signal ? { signal } : {});
  if (!response.ok) {
    throw new Error(`version request failed: ${response.status}`);
  }
  return (await response.json()) as VersionInfo;
}
