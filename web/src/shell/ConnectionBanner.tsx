import { useLiveness } from '@/shell/useLiveness';
import { TriangleAlert, WifiOff } from '@/ui/icons';

/**
 * The loud one.
 *
 * "A dashboard that silently shows stale numbers is worse than one that shows
 * an error" — the chip in the header is enough for a healthy stream, but it is
 * a 12px pill and someone watching this from across a room will not read it.
 * When the stream is gone the whole page has to say so, because every number
 * below is then a claim about the past wearing the present tense.
 *
 * Only `down` and `reconnecting` qualify. A degraded component is reported by
 * the chip and by the platform panel; shouting about it would spend the alarm
 * everyone needs to still believe when the feed actually stops.
 */
export function ConnectionBanner() {
  const liveness = useLiveness();
  if (liveness.status !== 'down' && liveness.status !== 'reconnecting') return null;

  const down = liveness.status === 'down';
  const Icon = down ? WifiOff : TriangleAlert;

  return (
    <div
      role="status"
      className={`flex flex-wrap items-center gap-2 border-b px-4 py-2.5 text-sm font-bold sm:px-6 lg:px-8 ${
        down ? 'border-danger-line bg-danger-soft text-danger' : 'border-line bg-sidebar text-warning'
      }`}
    >
      <Icon aria-hidden="true" className="size-4 shrink-0" strokeWidth={2.25} />
      {down ? 'Live stream disconnected — everything below is the last state measured, not the current one.' : 'Live stream interrupted — reconnecting, and the snapshot refetches when it returns.'}
      {liveness.detail !== null && (
        <span className="text-body font-normal">{liveness.detail}</span>
      )}
    </div>
  );
}
