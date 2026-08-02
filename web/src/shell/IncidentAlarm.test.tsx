import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { IncidentAlarm } from '@/shell/IncidentAlarm';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';

const playHooter = vi.hoisted(() => vi.fn());
vi.mock('@/shell/hooter', () => ({ playHooter, primeHooter: vi.fn() }));

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readyState = 0;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, Set<EventListener>>();

  constructor(readonly url: string | URL) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: EventListener): void {
    const set = this.listeners.get(type) ?? new Set<EventListener>();
    set.add(listener);
    this.listeners.set(type, set);
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
  emit(): void {
    const event = new MessageEvent('snapshot.invalidate', {
      data: JSON.stringify({
        event_id: 'e1',
        ts: '2026-08-02T03:00:00Z',
        kind: 'snapshot.invalidate',
        resources: ['incidents'],
      }),
    });
    for (const listener of this.listeners.get('snapshot.invalidate') ?? []) listener(event);
  }
}

function incident(id: string, over: Record<string, unknown> = {}) {
  return {
    incident_id: id,
    opened_at: '2026-08-02T02:58:00Z',
    updated_at: '2026-08-02T03:00:00Z',
    state: 'OPEN',
    severity: 'HIGH',
    services: ['frontend'],
    origin_service: 'frontend',
    verdict_class: 'ATTACK',
    reason: 'Credential failures concentrated into machine-regular sources.',
    evidence: [],
    action: { decision_action: 'ALERT', effect_status: null, detail: 'Alert raised.' },
    confidence: { status: 'insufficient', value: null, note: 'Not calibrated.' },
    muted: false,
    explanation: null,
    honesty: 'REAL',
    ...over,
  };
}

function snapshot(incidents: ReturnType<typeof incident>[]) {
  return {
    status: 'ready',
    incidents,
    count: incidents.length,
    limit: 20,
    observation: {
      status: 'WATCHING',
      last_judged_at: '2026-08-02T03:00:00Z',
      age_seconds: 2,
      expected_within_seconds: 120,
      note: 'Live telemetry is being judged now.',
    },
    detail: null,
  };
}

let body = snapshot([incident('incident-1')]);

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  window.localStorage.clear();
  window.localStorage.setItem('sentinel.sound', 'on');
  playHooter.mockClear();
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderAlarm() {
  return render(
    <MemoryRouter>
      <SnapshotStreamProvider>
        <IncidentAlarm />
      </SnapshotStreamProvider>
    </MemoryRouter>,
  );
}

describe('IncidentAlarm', () => {
  it('stays silent on the first read, which is history rather than news', async () => {
    body = snapshot([incident('incident-1')]);
    renderAlarm();

    // Sounding here would mean the alarm is about the page opening. An operator
    // who reloads at the start of a shift would be blasted by yesterday.
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(playHooter).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('sounds and interrupts when a genuinely new attack arrives', async () => {
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([incident('incident-2'), incident('incident-1')]);
    FakeEventSource.instances[0]?.emit();

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(playHooter).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('alert')).toHaveTextContent(/attack/i);
  });

  it('does not sound for a low-severity incident that resolved itself', async () => {
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([
      incident('quiet-1', { severity: 'LOW', verdict_class: 'OPERATIONAL_FAULT', state: 'RESOLVED' }),
      incident('incident-1'),
    ]);
    FakeEventSource.instances[0]?.emit();

    // An alarm that fires for everything teaches an operator to ignore alarms,
    // which costs more than the alarm was worth.
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(playHooter).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the banner but stays quiet when the alarm is muted', async () => {
    window.localStorage.setItem('sentinel.sound', 'off');
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([incident('incident-2'), incident('incident-1')]);
    FakeEventSource.instances[0]?.emit();

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(playHooter).not.toHaveBeenCalled();
  });
});
