import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ScenarioActivity } from '@/api/activity';
import type { IncidentFeedItem } from '@/contracts/types';
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

function incident(id: string, over: Record<string, unknown> = {}): IncidentFeedItem {
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
let activityBody: ScenarioActivity = {
  in_flight: false,
  run_id: null,
  scenario_id: null,
  mode: null,
  state: null,
  started_at: null,
  evidence_start_at: null,
  evidence_end_at: null,
  evidence_cursor_at: null,
  progress: null,
  replay_incident: null,
  note: 'No scenario is running.',
};

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  window.localStorage.clear();
  window.localStorage.setItem('sentinel.sound', 'on');
  playHooter.mockClear();
  activityBody = { ...activityBody, in_flight: false, run_id: null, replay_incident: null };
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const responseBody = url.includes('/api/activity') ? activityBody : body;
      return Promise.resolve(new Response(JSON.stringify(responseBody), { status: 200 }));
    }),
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
    act(() => FakeEventSource.instances[0]?.emit());

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
    act(() => FakeEventSource.instances[0]?.emit());

    // An alarm that fires for everything teaches an operator to ignore alarms,
    // which costs more than the alarm was worth.
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(playHooter).not.toHaveBeenCalled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('sounds for a new active code or operational fault handed to a person', async () => {
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([
      incident('code-1', {
        severity: 'LOW',
        verdict_class: 'CODE_CONFIG_FAULT',
        action: {
          decision_action: 'ESCALATE_TO_HUMAN',
          effect_status: null,
          detail: 'The broken change needs an operator.',
        },
      }),
      incident('incident-1'),
    ]);
    act(() => FakeEventSource.instances[0]?.emit());

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/code config fault/i));
    expect(playHooter).toHaveBeenCalledTimes(1);
  });

  it('sounds when an existing incident becomes actionable', async () => {
    body = snapshot([
      incident('changing-1', {
        severity: 'LOW',
        verdict_class: null,
        action: { decision_action: 'ALERT', effect_status: null, detail: 'Watching.' },
      }),
    ]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([
      incident('changing-1', {
        severity: 'LOW',
        verdict_class: 'OPERATIONAL_FAULT',
        updated_at: '2026-08-02T03:00:10Z',
        action: {
          decision_action: 'ESCALATE_TO_HUMAN',
          effect_status: null,
          detail: 'Operator action is required.',
        },
      }),
    ]);
    act(() => FakeEventSource.instances[0]?.emit());

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/operational fault/i));
    expect(playHooter).toHaveBeenCalledTimes(1);
  });

  it('does not repeat the alarm for evidence-only revisions of the same conclusion', async () => {
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([incident('incident-1', { updated_at: '2026-08-02T03:00:10Z' })]);
    act(() => FakeEventSource.instances[0]?.emit());

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(playHooter).not.toHaveBeenCalled();
  });

  it('interrupts visibly but silently when the producer is already stale on page load', async () => {
    body = {
      ...snapshot([incident('incident-1')]),
      observation: {
        status: 'STALE' as const,
        last_judged_at: '2026-08-02T02:00:00Z',
        age_seconds: 3600,
        expected_within_seconds: 120,
        note: 'Nothing has been judged recently.',
      },
    };
    renderAlarm();

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/live judgement stopped/i));
    expect(playHooter).not.toHaveBeenCalled();
  });

  it('sounds when live judgement transitions from watching to stale', async () => {
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = {
      ...snapshot([incident('incident-1')]),
      observation: {
        status: 'STALE' as const,
        last_judged_at: '2026-08-02T02:00:00Z',
        age_seconds: 3600,
        expected_within_seconds: 120,
        note: 'Nothing has been judged recently.',
      },
    };
    act(() => FakeEventSource.instances[0]?.emit());

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/live judgement stopped/i));
    expect(playHooter).toHaveBeenCalledTimes(1);
  });

  it('sounds once when a new replay run crosses a verified attack conclusion', async () => {
    body = snapshot([incident('incident-1')]);
    activityBody = {
      ...activityBody,
      in_flight: true,
      run_id: 'run-replay-1',
      scenario_id: 'combo_night',
      mode: 'REPLAY',
      state: 'RUNNING',
      replay_incident: null,
    };
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));

    activityBody = { ...activityBody, replay_incident: incident('incident-1') };
    act(() => FakeEventSource.instances[0]?.emit());

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/replay reached attack/i));
    expect(playHooter).toHaveBeenCalledTimes(1);

    act(() => FakeEventSource.instances[0]?.emit());
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
    expect(playHooter).toHaveBeenCalledTimes(1);
  });

  it('shows the banner but stays quiet when the alarm is muted', async () => {
    window.localStorage.setItem('sentinel.sound', 'off');
    body = snapshot([incident('incident-1')]);
    renderAlarm();
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    body = snapshot([incident('incident-2'), incident('incident-1')]);
    act(() => FakeEventSource.instances[0]?.emit());

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(playHooter).not.toHaveBeenCalled();
  });
});
