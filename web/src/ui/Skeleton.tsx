/**
 * A loading placeholder (`~/brand-kit/patterns.md`, "States — design all four").
 *
 * The shimmer lives in `index.css` as `.skeleton` because it needs a keyframe;
 * the kit's global `prefers-reduced-motion` block already stills it.
 *
 * It is deliberately `aria-hidden` with no text: the surface that owns it
 * announces the load through a live region, and a screen reader reading out a
 * row of grey rectangles is noise, not information.
 */
export function Skeleton({ className = '' }: { className?: string }) {
  return <span aria-hidden="true" className={`skeleton block ${className}`} />;
}

/**
 * The common case: several lines of pending text. `label` is what the assistive
 * technology hears in place of the rectangles.
 */
export function SkeletonText({
  lines = 3,
  label,
  className = '',
}: {
  lines?: number;
  label: string;
  className?: string;
}) {
  return (
    <div className={`flex flex-col gap-2 ${className}`} role="status" aria-busy="true">
      <span className="sr-only">{label}</span>
      {Array.from({ length: lines }, (_, index) => (
        // A ragged last line reads as text rather than as a block of boxes.
        <Skeleton key={index} className={`h-3 rounded-sm ${index === lines - 1 ? 'w-2/3' : ''}`} />
      ))}
    </div>
  );
}
