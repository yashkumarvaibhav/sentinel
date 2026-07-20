import { DecompositionPreview } from '@/components/DecompositionPreview';

const LEGEND = [
  { label: 'explained base', className: 'bg-base' },
  { label: 'explained by event', className: 'bg-event' },
  { label: 'unexplained residual', className: 'bg-residual' },
];

export function App() {
  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col gap-10 px-6 py-16">
      <header className="flex flex-col gap-3">
        <h1 className="text-4xl font-semibold tracking-tight">Sentinel</h1>
        <p className="text-ink-muted max-w-xl text-balance">
          Context-aware autonomous observability. An event explains volume, not behavior — so
          every surge is split into what the world explains and what it cannot.
        </p>
      </header>

      <section className="bg-surface-raised flex flex-col gap-4 rounded-xl p-5">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-medium tracking-wide uppercase">Decomposition</h2>
          <span
            className="border-ink-muted/40 text-ink-muted rounded border px-2 py-0.5 text-[11px] tracking-wider uppercase"
            title="Illustrative shape — this build has no telemetry pipeline yet"
          >
            Simulated
          </span>
        </div>

        <DecompositionPreview />

        <ul className="text-ink-muted flex flex-wrap gap-x-5 gap-y-2 text-xs">
          {LEGEND.map((item) => (
            <li key={item.label} className="flex items-center gap-2">
              <span className={`inline-block size-2.5 rounded-full ${item.className}`} />
              {item.label}
            </li>
          ))}
        </ul>
      </section>

      <footer className="text-ink-muted mt-auto text-xs">
        Foundation build — the pipeline, testbed and command center are not wired up yet.
      </footer>
    </div>
  );
}
