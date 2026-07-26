import { DecompositionPreview } from '@/components/DecompositionPreview';
import { PlatformStatus } from '@/components/PlatformStatus';
import { CommandShell } from '@/shell/CommandShell';

const LEGEND = [
  { label: 'explained base', className: 'bg-base' },
  { label: 'explained by event', className: 'bg-event' },
  { label: 'unexplained residual', className: 'bg-residual' },
];

export function App() {
  return (
    <CommandShell>
      <div className="flex flex-col gap-10">
        <header className="flex flex-col gap-3">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Command center</h1>
          <p className="text-body max-w-xl text-balance">
            An event explains volume, not behavior — so every surge is split into what the world
            explains and what it cannot. The part nothing explains is the product.
          </p>
        </header>

        <section className="border-line bg-raised flex flex-col gap-4 rounded-xl border p-5">
          <div className="flex items-baseline justify-between gap-4">
            <h2 className="text-sm font-medium tracking-wide uppercase">Decomposition</h2>
            <span
              className="border-line text-muted rounded border px-2 py-0.5 text-[11px] tracking-wider uppercase"
              title="Illustrative shape — the decomposition hero chart over live data lands in 6.2"
            >
              Simulated
            </span>
          </div>

          <DecompositionPreview />

          <ul className="text-muted flex flex-wrap gap-x-5 gap-y-2 text-xs">
            {LEGEND.map((item) => (
              <li key={item.label} className="flex items-center gap-2">
                <span className={`inline-block size-2.5 rounded-full ${item.className}`} />
                {item.label}
              </li>
            ))}
          </ul>
        </section>

        <PlatformStatus />

        <footer className="text-muted text-xs">
          Command shell only. The decomposition hero, KPI strip, incident feed and causal graph
          land across the rest of Phase 6.
        </footer>
      </div>
    </CommandShell>
  );
}
