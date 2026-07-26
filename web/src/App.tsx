import { DecompositionPanel } from '@/components/DecompositionPanel';
import { PlatformStatus } from '@/components/PlatformStatus';
import { CommandShell } from '@/shell/CommandShell';

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

        <DecompositionPanel />

        <PlatformStatus />

        <footer className="text-muted text-xs">
          The decomposition hero reads the platform's own store. The KPI strip, incident feed and
          causal graph land across the rest of Phase 6.
        </footer>
      </div>
    </CommandShell>
  );
}
