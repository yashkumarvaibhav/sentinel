import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useEventStream } from '@/shell/useEventStream';

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(url: string | URL) {
    this.url = url.toString();
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener): void {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.readyState = 2;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  disconnect(): void {
    this.readyState = 0;
    this.onerror?.(new Event('error'));
  }

  emit(data: string): void {
    const event = new MessageEvent('snapshot.invalidate', { data });
    for (const listener of this.listeners.get('snapshot.invalidate') ?? []) {
      listener(event);
    }
  }
}

const INVALIDATION = JSON.stringify({
  event_id: 'event-1',
  ts: '2026-07-26T00:00:00Z',
  kind: 'snapshot.invalidate',
  resources: ['incidents'],
});

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useEventStream', () => {
  it('uses native reconnects and refetches snapshots on every open', async () => {
    const refetchSnapshots = vi.fn();
    const { result } = renderHook(() => useEventStream({ refetchSnapshots }));
    const source = FakeEventSource.instances[0];
    expect(source?.url).toBe('/stream/events');

    act(() => source?.open());
    await waitFor(() => expect(result.current.status).toBe('open'));
    expect(refetchSnapshots).toHaveBeenCalledTimes(1);

    act(() => source?.disconnect());
    expect(result.current.status).toBe('reconnecting');

    act(() => source?.open());
    await waitFor(() => expect(refetchSnapshots).toHaveBeenCalledTimes(2));
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('parses typed invalidations and closes the source on cleanup', () => {
    const onEvent = vi.fn();
    const { unmount } = renderHook(() =>
      useEventStream({ refetchSnapshots: vi.fn(), onEvent }),
    );
    const source = FakeEventSource.instances[0];

    act(() => source?.open());
    act(() => source?.emit(INVALIDATION));

    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'snapshot.invalidate', resources: ['incidents'] }),
    );

    unmount();
    expect(source?.readyState).toBe(2);
  });

  it('keeps the connection open but exposes an invalid typed payload', () => {
    const { result } = renderHook(() =>
      useEventStream({ refetchSnapshots: vi.fn(), onEvent: vi.fn() }),
    );
    const source = FakeEventSource.instances[0];

    act(() => source?.open());
    act(() => source?.emit('{"kind":"wrong"}'));

    expect(result.current.status).toBe('open');
    expect(result.current.error).toMatch(/stream event/i);
  });
});
