/**
 * The Sentinel mark.
 *
 * A shield — the thing standing watch — with the product's own thesis drawn
 * inside it: a flat expected band, and one spike breaking above the line that
 * the band does not explain. That residual is the product, so it is the only
 * part of the mark that moves.
 *
 * It carries `--brand` (`#3fada8`), never `--accent`. The house rule is that
 * the brand swatch is the identity and fails AA at 2.7:1 on white, so it may
 * hold a logo and large fills but never text, a link, or a focus ring. Drawn
 * rather than fetched: an inline SVG costs no request, stays crisp at any size,
 * and cannot 404 the way a missing PNG can.
 */
export function BrandMark({ className = '' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={className}
      stroke="var(--brand)"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M12 2.6 19.5 6v6c0 4.4-3.2 7.5-7.5 9.4C7.7 19.5 4.5 16.4 4.5 12V6Z" />
      {/* expected band, then the part it cannot explain */}
      <path d="M7.6 13.6h2.2l1.5-4.2 1.6 6 1.2-1.8h2.3" />
    </svg>
  );
}
