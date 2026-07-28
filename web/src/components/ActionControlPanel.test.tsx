import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ActionControlPanel } from '@/components/ActionControlPanel';
import { OperatorCredentialProvider } from '@/shell/OperatorCredential';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';
import { ACTION_RESPONSE } from '@/test/actionControlFixture';

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPanel() {
  return render(
    <OperatorCredentialProvider>
      <SnapshotStreamProvider>
        <ActionControlPanel incidentId="incident-proof-1" />
      </SnapshotStreamProvider>
    </OperatorCredentialProvider>,
  );
}

it('keeps the credential in memory and submits only intent identity', async () => {
  const rejected = {
    ...ACTION_RESPONSE,
    control: {
      ...ACTION_RESPONSE.control,
      state: 'REJECTED',
      rejected_by: 'on-call-primary',
      rejected_at: '2026-07-28T12:01:00Z',
      updated_at: '2026-07-28T12:01:00Z',
    },
  };
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'secret required' }), { status: 401 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(ACTION_RESPONSE), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify(rejected), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  const localStorage = vi.spyOn(Storage.prototype, 'setItem');
  const sessionStorage = vi.spyOn(window.sessionStorage, 'setItem');

  renderPanel();
  await waitFor(() => expect(screen.getByLabelText(/operator credential/i)).toBeVisible());
  fireEvent.change(screen.getByLabelText(/operator credential/i), {
    target: { value: 'memory-only-secret' },
  });
  fireEvent.click(screen.getByRole('button', { name: /unlock action controls/i }));

  await waitFor(() => expect(screen.getByText('Rate limit')).toBeVisible());
  expect(screen.getByText('route/login')).toBeVisible();
  expect(screen.getByText(/10% measured blast radius/i)).toBeVisible();
  expect(screen.getAllByText('Passed')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: /reject plan/i }));

  await waitFor(() => expect(screen.getByText(/rejected by on-call-primary/i)).toBeVisible());
  const mutation = fetch.mock.calls[2]!;
  expect(mutation[0]).toBe('/api/incidents/incident-proof-1/action');
  const init = mutation[1] as RequestInit;
  expect(init.headers).toEqual({ 'content-type': 'application/json', 'x-sentinel-secret': 'memory-only-secret' });
  expect(typeof init.body).toBe('string');
  expect(JSON.parse(init.body as string)).toEqual({
    incident_id: 'incident-proof-1',
    plan_revision: 1,
    intent: 'REJECT',
  });
  expect(localStorage).not.toHaveBeenCalledWith(expect.stringMatching(/credential|secret/i), expect.anything());
  expect(sessionStorage).not.toHaveBeenCalled();
});

it('refuses to turn one interim identity into two approvals', async () => {
  const destructive = {
    ...ACTION_RESPONSE,
    control: {
      ...ACTION_RESPONSE.control,
      rung: {
        ...ACTION_RESPONSE.control.rung,
        rung_id: 'isolate-workload',
        ladder_id: 'fault',
        actuator: 'KUBERNETES',
        action_kind: 'ISOLATE',
        parameters: { nodes: 'worker-0' },
        required_approval_count: 2,
        maximum_blast_fraction: 1,
      },
      plan: {
        ...ACTION_RESPONSE.control.plan,
        actuator: 'KUBERNETES',
        action_kind: 'ISOLATE',
        target_ref: 'deployment/payment',
        parameters: { nodes: 'worker-0' },
        estimated_blast_fraction: 1,
      },
    },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(JSON.stringify(destructive), { status: 200 }))),
  );

  renderPanel();

  await waitFor(() => expect(screen.getByText(/two distinct approvers required/i)).toBeVisible());
  expect(screen.getByRole('button', { name: /approve plan/i })).toBeDisabled();
});
