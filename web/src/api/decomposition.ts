import type { DecompFrame } from '@/contracts/types';

/**
 * One window of decomposition frames, exactly as the gateway serves it.
 *
 * `truncated` is not decoration. The server caps a window at 5000 frames, and a
 * chart that ignored this field would draw a partial window as though it were
 * the whole story — which, in a product whose entire claim is that the residual
 * is visible, would be the one lie that matters.
 */
export interface DecompositionWindow {
  service: string;
  signal: string;
  start: string;
  end: string;
  count: number;
  truncated: boolean;
  limit: number;
  frames: DecompFrame[];
}

export interface DecompositionQuery {
  service: string;
  signal: string;
  start?: Date;
  end?: Date;
  limit?: number;
}

export class DecompositionUnavailableError extends Error {}

/**
 * Read one window.
 *
 * A 503 is its own error type because it means something different from a
 * network failure: the gateway is up and telling us it has no store attached.
 * The UI says "the decomposition store is not attached" rather than "could not
 * load", because those send a reader to two different places.
 */
export async function fetchDecomposition(
  query: DecompositionQuery,
  signal?: AbortSignal,
): Promise<DecompositionWindow> {
  const params = new URLSearchParams({ service: query.service, signal: query.signal });
  if (query.start) params.set('start', query.start.toISOString());
  if (query.end) params.set('end', query.end.toISOString());
  if (query.limit !== undefined) params.set('limit', String(query.limit));

  const response = await fetch(`/api/decomposition?${params.toString()}`, signal ? { signal } : {});
  if (response.status === 503) {
    const body = (await response.json()) as { detail?: string };
    throw new DecompositionUnavailableError(
      body.detail ?? 'no decomposition store is attached to this gateway',
    );
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `decomposition request failed: ${response.status}`);
  }
  return (await response.json()) as DecompositionWindow;
}

/**
 * Whether a frame's own numbers add up.
 *
 * The server sends the parts rather than a summary precisely so this is
 * checkable on the client, and checking it is the difference between showing a
 * decomposition and asserting one. A frame that fails is a bug somewhere
 * upstream and is surfaced rather than drawn.
 */
export function isCoherent(frame: DecompFrame, tolerance = 1e-6): boolean {
  const parts = frame.explained_base + frame.explained_event + frame.residual;
  return Math.abs(frame.observed - parts) <= tolerance;
}
