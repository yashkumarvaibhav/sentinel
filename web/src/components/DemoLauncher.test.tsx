import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { parseLabFeed } from '@/api/lab';
import { DemoLauncher } from '@/components/DemoLauncher';
import { OperatorCredentialProvider } from '@/shell/OperatorCredential';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';

const HONESTY = {
  telemetry: 'REAL — recorded from the testbed, replayed byte for byte.',
  stimulus: 'SIMULATED — the faults were injected when this was recorded.',
  reproducibility: 'Bit-exact: the same capture always produces the same decisions.',
};

const SCENARIO = {
  scenario_id: 'combo_night',
  name: 'Championship night',
  description: 'An attack hiding inside an explained surge.',
  modes: ['REPLAY', 'LIVE'],
  live_duration_seconds: 1004,
};

function feed(overrides: Record<string, unknown> = {}) {
  return {
    status: 'READY',
    scenarios: [SCENARIO],
    runs: [],
    runner_attached: true,
    note: 'Pick a scenario.',
    ...overrides,
  };
}

function answer(body: unknown, status = 200): Response {
  return { status, json: () => Promise.resolve(body) } as Response;
}

function ui() {
  return (
    <OperatorCredentialProvider>
      <SnapshotStreamProvider>
        <DemoLauncher />
      </SnapshotStreamProvider>
    </OperatorCredentialProvider>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the demo launcher', () => {
  it('renders what can be fired and how long live costs', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(answer(feed()));

    render(ui());

    expect(await screen.findByText('Championship night')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Replay a capture/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: /about 17 min/ })).toBeInTheDocument();
  });

  it('says plainly when no runner is attached instead of offering dead buttons', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      answer(
        feed({
          status: 'UNAVAILABLE',
          runner_attached: false,
          note: 'No lab runner is attached, so a request would queue work nothing will ever claim.',
        }),
      ),
    );

    render(ui());

    expect(await screen.findByText(/no lab runner is attached/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Replay a capture/ })).toBeDisabled();
  });

  it('refuses a second scenario while one is running, and says why', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      answer(
        feed({
          status: 'BUSY',
          note: 'combo_night is already running. A second scenario would put two sets of injected faults into one stretch of telemetry.',
          runs: [
            {
              run_id: 'run-1',
              scenario_id: 'combo_night',
              mode: 'LIVE',
              state: 'RUNNING',
              requested_at: '2026-08-01T12:00:00Z',
              started_at: '2026-08-01T12:00:01Z',
              finished_at: null,
              incident_id: null,
              detail: 'Driving the testbed.',
              honesty: HONESTY,
            },
          ],
        }),
      ),
    );

    render(ui());

    expect(await screen.findByText(/two sets of injected faults/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Replay a capture/ })).toBeDisabled();
    expect(screen.getByText('RUNNING')).toBeInTheDocument();
  });

  it('fires a scenario and shows what came back', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(answer(feed()))
      .mockResolvedValueOnce(
        answer(
          feed({
            status: 'BUSY',
            note: 'combo_night is already running.',
            runs: [
              {
                run_id: 'run-2',
                scenario_id: 'combo_night',
                mode: 'REPLAY',
                state: 'QUEUED',
                requested_at: '2026-08-01T12:00:00Z',
                started_at: null,
                finished_at: null,
                incident_id: null,
                detail: 'Queued. The runner claims it; this endpoint never executes one.',
                honesty: HONESTY,
              },
            ],
          }),
        ),
      );

    render(ui());
    await userEvent.click(await screen.findByRole('button', { name: /Replay a capture/ }));

    await waitFor(() => expect(screen.getByText('QUEUED')).toBeInTheDocument());
    const [, request] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(request.method).toBe('POST');
    expect(JSON.parse(request.body as string)).toEqual({
      scenario_id: 'combo_night',
      mode: 'REPLAY',
    });
    expect(screen.getByText(/never executes one/)).toBeInTheDocument();
  });

  it('asks for the credential rather than reporting a failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(answer({}, 401));

    render(ui());

    expect(await screen.findByText(/needs the operator credential/i)).toBeInTheDocument();
    // Nothing has been offered yet, so there is nothing to have been refused.
    expect(screen.queryByText(/refused/i)).not.toBeInTheDocument();
  });

  it('says a credential was refused rather than re-showing a blank prompt', async () => {
    // The bug this pins: a wrong key produced the same screen as no key at all,
    // so entering one and being rejected looked exactly like nothing happening.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(answer({}, 401));

    render(ui());
    await screen.findByText(/needs the operator credential/i);

    await userEvent.type(await screen.findByLabelText('Operator credential'), 'wrong-key');
    await userEvent.click(screen.getByRole('button', { name: /unlock/i }));

    expect(await screen.findByText(/that credential was refused/i)).toBeInTheDocument();
  });

  it('shows a succeeded run as a link to the incident it produced', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      answer(
        feed({
          runs: [
            {
              run_id: 'run-3',
              scenario_id: 'combo_night',
              mode: 'REPLAY',
              state: 'SUCCEEDED',
              requested_at: '2026-08-01T12:00:00Z',
              started_at: '2026-08-01T12:00:01Z',
              finished_at: '2026-08-01T12:00:09Z',
              incident_id: 'incident-abc',
              detail: 'Replayed and published.',
              honesty: HONESTY,
            },
          ],
        }),
      ),
    );

    render(ui());

    const link = await screen.findByRole('link', { name: /open the incident/i });
    expect(link).toHaveAttribute('href', '/incidents/incident-abc');
  });
});

describe('the launcher parser', () => {
  it('refuses a payload that is watching and finished at the same time', () => {
    expect(() =>
      parseLabFeed(
        feed({
          runs: [
            {
              run_id: 'run-4',
              scenario_id: 'combo_night',
              mode: 'LIVE',
              state: 'RUNNING',
              requested_at: '2026-08-01T12:00:00Z',
              started_at: '2026-08-01T12:00:01Z',
              finished_at: '2026-08-01T12:00:09Z',
              incident_id: null,
              detail: 'Still going, apparently.',
              honesty: HONESTY,
            },
          ],
        }),
      ),
    ).toThrow(/terminal run state/);
  });

  it('refuses a ready launcher that admits it has no runner', () => {
    expect(() => parseLabFeed(feed({ runner_attached: false }))).toThrow(/cannot be ready/);
  });

  it('refuses a live scenario that will not say how long it takes', () => {
    expect(() =>
      parseLabFeed(feed({ scenarios: [{ ...SCENARIO, live_duration_seconds: null }] })),
    ).toThrow(/how long live takes/);
  });
});
