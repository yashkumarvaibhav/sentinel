import { DecompositionPanel } from '@/components/DecompositionPanel';
import { KpiStrip } from '@/components/KpiStrip';
import { IncidentFeed } from '@/components/IncidentFeed';
import { CausalGraphPanel } from '@/components/CausalGraph';
import { PlatformStatus } from '@/components/PlatformStatus';
import { InfoPopover } from '@/ui/InfoPopover';

export function App() {
  return (
    <div className="flex flex-col gap-8">
      {/* The thesis used to be two lines of prose under the title, so the first
          paint of a command center was mostly explanation. It is not deleted —
          it is the reason every number below means anything — it just stops
          being the first thing an operator has to read past on every visit. */}
      <header className="flex items-center gap-2">
        <h1 className="font-serif">Command center</h1>
        <InfoPopover label="What this screen is showing">
          An event explains volume, not behavior — so every surge is split into what the world
          explains and what it cannot. The part nothing explains is the product.
        </InfoPopover>
      </header>

      <DecompositionPanel />

      <KpiStrip />

      <IncidentFeed />

      <CausalGraphPanel />

      <PlatformStatus />
    </div>
  );
}
