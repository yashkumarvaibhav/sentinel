import { useEffect, useState } from 'react';

function absolute(iso: string): string {
  return `${new Intl.DateTimeFormat('en', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  }).format(new Date(iso))} UTC`;
}

function relative(iso: string, now: number): string {
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return 'unknown';
  if (seconds < 0) return 'just now';
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

/**
 * A timestamp that actually moves.
 *
 * The function this replaces was called `timeAgo` and returned an absolute UTC
 * stamp — so nothing on the screen ever visibly changed, and a page backed by a
 * live event stream read as a static report. That is a large part of why the
 * console looked frozen even while it was working.
 *
 * The absolute stamp is not thrown away; it moves into the tooltip and the
 * machine-readable `dateTime`, because "7 min ago" is the right answer for a
 * glance and the wrong one for an incident report.
 */
export function RelativeTime({ iso, className = '' }: { iso: string; className?: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    // Ten seconds: fast enough that "just now" becomes "20s ago" while you
    // watch, slow enough to be free. Reduced-motion is about animation, not
    // about lying to someone over how old their data is.
    const timer = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(timer);
  }, []);

  return (
    <time dateTime={iso} title={absolute(iso)} className={className}>
      {relative(iso, now)}
    </time>
  );
}
