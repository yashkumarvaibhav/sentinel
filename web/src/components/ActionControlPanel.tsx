import { useCallback, useEffect, useRef, useState } from 'react';

import {
  CredentialRequiredError,
  fetchActionControl,
  mutateActionControl,
} from '@/api/actionControl';
import type {
  ActionControlIntent,
  ActionControlResponse,
  ActionControlSnapshot,
} from '@/contracts/types';
import { OperatorCredentialPrompt } from '@/shell/OperatorCredential';
import { useOperatorCredential } from '@/shell/useOperatorCredential';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';

const ACTION_RESOURCES = ['actions', 'incidents'] as const;

type Load =
  | { state: 'loading' }
  | { state: 'locked'; detail?: string }
  | { state: 'ready'; response: ActionControlResponse }
  | { state: 'error'; detail: string };

function title(value: string): string {
  const rendered = value.replaceAll('_', ' ').toLowerCase();
  return rendered.charAt(0).toUpperCase() + rendered.slice(1);
}

function ttl(seconds: number): string {
  if (seconds % 60 === 0) return `${seconds / 60} min after application`;
  return `${seconds}s after application`;
}

function State({ control }: { control: ActionControlSnapshot }) {
  if (control.state === 'REJECTED') {
    return (
      <p className="text-bad text-xs">
        Rejected by {control.rejected_by} at{' '}
        {new Date(control.rejected_at ?? control.updated_at).toLocaleString('en', {
          timeZone: 'UTC',
        })}{' '}
        UTC. This plan revision is terminal.
      </p>
    );
  }
  if (control.state === 'APPLY_REQUESTED') {
    return (
      <p className="text-warn text-xs">
        Approval is durable. Execution is requested; no production effect is claimed until the
        actuator returns an authoritative outcome.
      </p>
    );
  }
  if (control.state === 'ROLLBACK_REQUESTED') {
    return (
      <p className="text-warn text-xs">
        Rollback is requested against the server-held plan and revert token. Undo is not claimed
        until the actuator verifies it.
      </p>
    );
  }
  if (control.state === 'APPLIED') {
    return (
      <p className="text-warn text-xs">
        The actuator reports the effect was applied. Delayed target verification is still pending,
        so this is not yet a verified effect.
      </p>
    );
  }
  if (control.state === 'ROLLED_BACK') {
    const proof = control.rollback_verification ?? null;
    if (proof === null) {
      return (
        <p className="text-warn text-xs">
          The actuator reports the server-held effect was reverted. Delayed protected-service SLO
          verification is still pending.
        </p>
      );
    }
    return (
      <div className="grid gap-1 text-xs">
        <p className={proof.status === 'VERIFIED' ? 'text-ok' : 'text-warn'}>
          Rollback recovery · {title(proof.status)} · {proof.detail}
        </p>
        <p className="text-muted">
          Checked availability + p95 latency across {proof.after.length} protected service
          {proof.after.length === 1 ? '' : 's'} · users restored:{' '}
          {proof.users_restored === null
            ? 'insufficient telemetry'
            : `${(proof.users_restored * 100).toFixed(2)}%`}
        </p>
      </div>
    );
  }
  if (control.latest_outcome !== null) {
    return (
      <p className="text-muted text-xs">
        Latest actuator state · {title(control.latest_outcome.status)} ·{' '}
        {control.latest_outcome.detail}
      </p>
    );
  }
  return (
    <p className="text-muted text-xs">
      {title(control.state)}. No actuator outcome is attached to this plan revision.
    </p>
  );
}

export function ActionControlPanel({ incidentId }: { incidentId: string }) {
  const { credential, clearCredential } = useOperatorCredential();
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [pending, setPending] = useState<ActionControlIntent | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);
  const queued = useRef(false);
  const controller = useRef<AbortController | null>(null);

  const refetch = useCallback((): Promise<void> => {
    if (inFlight.current !== null) {
      queued.current = true;
      return inFlight.current;
    }
    const requestController = new AbortController();
    controller.current = requestController;
    const request = fetchActionControl(incidentId, credential, requestController.signal)
      .then((response) => {
        if (!requestController.signal.aborted) setLoad({ state: 'ready', response });
      })
      .catch((error: unknown) => {
        if (requestController.signal.aborted) return;
        if (error instanceof CredentialRequiredError) {
          setLoad({ state: 'locked' });
          return;
        }
        setLoad({
          state: 'error',
          detail: error instanceof Error ? error.message : String(error),
        });
      })
      .finally(() => {
        inFlight.current = null;
        controller.current = null;
        if (queued.current && !requestController.signal.aborted) {
          queued.current = false;
          void refetch();
        }
      });
    inFlight.current = request;
    return request;
  }, [credential, incidentId]);

  useSnapshotInvalidation(ACTION_RESOURCES, refetch);
  useEffect(() => {
    setLoad({ state: 'loading' });
    void refetch();
    return () => controller.current?.abort();
  }, [refetch]);

  const mutate = async (intent: ActionControlIntent, control: ActionControlSnapshot) => {
    setPending(intent);
    try {
      const response = await mutateActionControl(
        incidentId,
        control.plan_revision,
        intent,
        credential,
      );
      setLoad({ state: 'ready', response });
    } catch (error: unknown) {
      if (error instanceof CredentialRequiredError) {
        clearCredential();
        setLoad({
          state: 'locked',
          detail: 'The credential was not accepted.',
        });
      } else {
        setLoad({
          state: 'error',
          detail: error instanceof Error ? error.message : String(error),
        });
      }
    } finally {
      setPending(null);
    }
  };

  if (load.state === 'loading') {
    return (
      <p className="text-muted text-sm" role="status">
        Reading action controls…
      </p>
    );
  }
  if (load.state === 'locked') {
    return <OperatorCredentialPrompt detail={load.detail} />;
  }
  if (load.state === 'error') {
    return (
      <p className="text-bad text-sm" role="alert">
        {load.detail}
      </p>
    );
  }
  if (load.response.control === null) {
    if (load.response.status === 'degraded') {
      return (
        <p className="text-bad text-sm" role="alert">
          {load.response.message}
        </p>
      );
    }
    return (
      <div className="border-line bg-sidebar rounded-lg border p-4 text-sm">
        <strong>No server-held action plan.</strong>
        <p className="text-muted mt-1">{load.response.message}</p>
        <p className="text-muted mt-1 text-xs">
          Absence is not approval, rejection, or evidence that no action is needed.
        </p>
      </div>
    );
  }

  const control = load.response.control;
  const twoKeyUnavailable = control.rung.required_approval_count > 1;
  const awaiting = control.state === 'AWAITING_APPROVAL';
  const canRollback = control.state === 'APPLIED' || control.state === 'VERIFIED';
  return (
    <div className="border-line bg-raised grid gap-5 rounded-lg border p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-muted text-[10px] tracking-wide uppercase">
            Plan revision {control.plan_revision} · {control.plan.honesty}
          </p>
          <h3 className="mt-1 text-lg font-semibold">{title(control.rung.action_kind)}</h3>
          <p className="text-muted mt-1 text-xs">{control.rung.reason}</p>
        </div>
        <span className="border-line rounded border px-2 py-1 text-xs">{title(control.state)}</span>
      </div>

      <dl className="grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-muted">Target</dt>
          <dd className="mt-1 font-mono">{control.plan.target_ref}</dd>
        </div>
        <div>
          <dt className="text-muted">Measured blast radius</dt>
          <dd className="mt-1">
            {Math.round(control.plan.estimated_blast_fraction * 100)}% measured blast radius
          </dd>
        </div>
        <div>
          <dt className="text-muted">TTL</dt>
          <dd className="mt-1">{ttl(control.rung.ttl_seconds)}</dd>
        </div>
        <div>
          <dt className="text-muted">Approval policy</dt>
          <dd className="mt-1">
            {control.rung.required_approval_count === 0
              ? 'Autonomous'
              : `${control.rung.required_approval_count} distinct approver${
                  control.rung.required_approval_count === 1 ? '' : 's'
                } required`}
          </dd>
        </div>
      </dl>

      <div>
        <p className="text-muted text-[10px] tracking-wide uppercase">Deterministic gates</p>
        <ul className="mt-2 grid gap-2 sm:grid-cols-2">
          {control.guard_results.map((gate) => (
            <li className="border-line bg-sidebar rounded border p-3 text-xs" key={gate.gate_id}>
              <div className="flex items-center justify-between gap-2">
                <strong>{title(gate.gate_id)}</strong>
                <span className={gate.status === 'PASSED' ? 'text-ok' : 'text-bad'}>
                  {title(gate.status)}
                </span>
              </div>
              <p className="text-muted mt-1">{gate.detail}</p>
            </li>
          ))}
        </ul>
      </div>

      <State control={control} />

      {twoKeyUnavailable && awaiting && (
        <p className="border-warn text-warn rounded border p-3 text-xs">
          Two distinct approvers required. The interim shared credential represents one identity, so
          this destructive plan remains refused until OIDC and two-key approval land.
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        {awaiting && (
          <>
            <button
              className="bg-accent text-accent-contrast min-h-11 rounded px-4 text-sm font-medium disabled:opacity-50"
              disabled={pending !== null || twoKeyUnavailable}
              onClick={() => void mutate('APPROVE', control)}
              type="button"
            >
              {pending === 'APPROVE' ? 'Approving…' : 'Approve plan'}
            </button>
            <button
              className="border-line min-h-11 rounded border px-4 text-sm font-medium disabled:opacity-50"
              disabled={pending !== null}
              onClick={() => void mutate('REJECT', control)}
              type="button"
            >
              {pending === 'REJECT' ? 'Rejecting…' : 'Reject plan'}
            </button>
          </>
        )}
        {canRollback && (
          <button
            className="border-bad text-bad min-h-11 rounded border px-4 text-sm font-medium disabled:opacity-50"
            disabled={pending !== null}
            onClick={() => void mutate('ROLLBACK', control)}
            type="button"
          >
            {pending === 'ROLLBACK' ? 'Requesting rollback…' : 'Rollback verified effect'}
          </button>
        )}
      </div>
    </div>
  );
}
