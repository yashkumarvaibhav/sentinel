import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';

import { fetchIncidents } from '@/api/incidents';
import type { IncidentFeedItem, ObservationFreshness } from '@/contracts/types';
import { playHooter } from '@/shell/hooter';
import { useSnapshotInvalidation } from '@/shell/useSnapshotStream';
import { useSound } from '@/shell/useSound';
import { ShieldAlert, TriangleAlert } from '@/ui/icons';
import { verdictIcon } from '@/ui/verdict';

const INCIDENT_RESOURCES = ['incidents'] as const;

/**
 * What is loud enough to interrupt someone.
 *
 * A confirmed attack, or anything the platform rated critical or high. A `LOW`
 * operational fault that resolves itself is real information and belongs in the
 * feed, but an alarm that sounds for it teaches the operator to ignore alarms —
 * which costs more than the alarm was ever worth.
 */
function isAlarming(item: IncidentFeedItem): boolean {
  if (item.state === 'RESOLVED' || item.muted) return false;
  if (
    item.verdict_class === 'ATTACK' ||
    item.verdict_class === 'COMBINATION' ||
    item.severity === 'CRITICAL' ||
    item.severity === 'HIGH'
  )
    return true;
  return (
    (item.verdict_class === 'OPERATIONAL_FAULT' ||
      item.verdict_class === 'CODE_CONFIG_FAULT') &&
    (item.action.decision_action === 'ACT' ||
      item.action.decision_action === 'ESCALATE_TO_HUMAN' ||
      item.action.decision_action === 'AUTO_CONTAIN_THEN_ESCALATE')
  );
}

function alarmSignature(item: IncidentFeedItem): string | null {
  if (!isAlarming(item)) return null;
  // The incident store publishes a new revision whenever live evidence moves.
  // Audio belongs to a newly alarming conclusion, not every measured update
  // while the same conclusion remains active.
  return [item.verdict_class, item.severity, item.action.decision_action].join(':');
}

type AlarmEvent =
  | { kind: 'incident'; incident: IncidentFeedItem }
  | { kind: 'observation'; observation: ObservationFreshness };

/**
 * The interrupt: a banner and a siren when the platform confirms something that
 * warrants a person.
 *
 * The first fetch **seeds** what has been seen without sounding. Anything else
 * would mean every page load blasts the history, and the alarm would be about
 * the page opening rather than about anything happening.
 */
export function IncidentAlarm() {
  const { enabled } = useSound();
  const [alarm, setAlarm] = useState<AlarmEvent | null>(null);
  const seen = useRef<Map<string, string | null> | null>(null);
  const observation = useRef<ObservationFreshness['status'] | null>(null);
  const soundEnabled = useRef(enabled);
  soundEnabled.current = enabled;

  const check = useCallback((): Promise<void> => {
    const controller = new AbortController();
    return fetchIncidents(controller.signal, 20)
      .then((response) => {
        if (response.status !== 'ready') return;
        const alarming = response.incidents.filter(isAlarming);
        const conclusions = new Map(
          response.incidents.map((item) => [item.incident_id, alarmSignature(item)]),
        );

        if (seen.current === null) {
          // First read of the session: remember, do not announce.
          seen.current = conclusions;
          observation.current = response.observation.status;
          // Historical incidents stay silent, but a producer that is not
          // watching *now* is current platform state and must remain visible.
          if (response.observation.status !== 'WATCHING') {
            setAlarm({ kind: 'observation', observation: response.observation });
          }
          return;
        }

        const previousObservation = observation.current;
        observation.current = response.observation.status;
        if (
          previousObservation === 'WATCHING' &&
          response.observation.status !== 'WATCHING'
        ) {
          setAlarm({ kind: 'observation', observation: response.observation });
          if (soundEnabled.current) playHooter();
        }

        const fresh = alarming.find(
          (item) => seen.current?.get(item.incident_id) !== alarmSignature(item),
        );
        for (const [incidentId, conclusion] of conclusions) {
          seen.current.set(incidentId, conclusion);
        }
        if (fresh === undefined) return;

        setAlarm({ kind: 'incident', incident: fresh });
        if (soundEnabled.current) playHooter();
      })
      .catch(() => {
        // The connection banner already reports an unreachable gateway; a
        // second complaint here would be noise about the same fact.
      });
  }, []);

  useSnapshotInvalidation(INCIDENT_RESOURCES, check);
  useEffect(() => {
    void check();
    // A dead producer emits no invalidation announcing its own death. Polling
    // the freshness snapshot is therefore the only way to turn silence into
    // an interrupt while the SSE connection itself remains healthy.
    const timer = window.setInterval(() => void check(), 10_000);
    return () => window.clearInterval(timer);
  }, [check]);

  if (alarm === null) return null;

  const incident = alarm.kind === 'incident' ? alarm.incident : null;
  const staleObservation = alarm.kind === 'observation' ? alarm.observation : null;
  const verdict = incident?.verdict_class ?? 'UNCLASSIFIED';
  const VerdictIcon = verdict === 'ATTACK' ? ShieldAlert : verdictIcon(verdict);

  return (
    // `alert` rather than `status`: this interrupts a screen reader, which is
    // the point. It is dismissible because an operator who has read it should
    // be able to get it off their screen without waiting for a state change.
    <div
      role="alert"
      className="border-danger-line bg-danger-soft flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-2.5 sm:px-6 lg:px-8"
    >
      <TriangleAlert
        aria-hidden="true"
        className="text-danger size-4 shrink-0"
        strokeWidth={2.25}
      />
      <p className="text-danger min-w-0 flex-1 text-sm font-bold">
        {incident === null ? (
          <>
            Live judgement stopped
            <span className="text-body ml-2 font-normal">
              {staleObservation?.note} Everything on the command screen is the last measured
              state until the producer resumes.
            </span>
          </>
        ) : (
          <>
            <VerdictIcon
              aria-hidden="true"
              className="mr-1.5 inline size-4"
              strokeWidth={2.25}
            />
            {incident.verdict_class === null
              ? 'Unclassified incident'
              : incident.verdict_class.replaceAll('_', ' ').toLowerCase()}
            {incident.origin_service !== null && ` on ${incident.origin_service}`}
            <span className="text-body ml-2 font-normal">{incident.reason}</span>
          </>
        )}
      </p>
      {/* Audio is opt-in because a browser will not play it before a user
          gesture, so an alarm that defaulted on would be silently disarmed.
          But an operator who has never armed it should be told that here,
          where they are already looking, rather than discovering it the next
          time something happens and nothing sounds. */}
      {!enabled && (
        <span className="text-muted shrink-0 text-xs">
          Sound is muted — arm the alarm in the header to hear the next one.
        </span>
      )}
      {incident !== null && (
        <Link
          to={`/incidents/${encodeURIComponent(incident.incident_id)}`}
          onClick={() => setAlarm(null)}
          className="text-accent shrink-0 text-xs font-bold underline decoration-transparent underline-offset-4 hover:decoration-current"
        >
          Open evidence proof
        </Link>
      )}
      <button
        type="button"
        onClick={() => setAlarm(null)}
        className="border-line text-ink hover:bg-hover flex size-11 shrink-0 items-center justify-center rounded-md border sm:size-8"
        aria-label="Dismiss this alarm"
      >
        <svg
          aria-hidden="true"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </div>
  );
}
