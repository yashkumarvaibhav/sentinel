import { DecompositionPanel } from '@/components/DecompositionPanel';
import { KpiStrip } from '@/components/KpiStrip';
import { IncidentFeed } from '@/components/IncidentFeed';
import { CausalGraphPanel } from '@/components/CausalGraph';
import { PlatformStatus } from '@/components/PlatformStatus';
import { InfoPopover } from '@/ui/InfoPopover';

/**
 * The command center.
 *
 * This was one vertical stack of five equally-weighted sections, which is what
 * made a live console read as a document: nothing was more important than
 * anything else, and the thing the product is *about* sat in the same box as
 * the build stamp.
 *
 * The grid says what matters. Reliability tiles run across the top because they
 * are a glance, not a read. Then the decomposition takes three columns of five
 * beside the live feed's two — the decomposition is the thesis and leads
 * (`UIUX_SPEC.md` §4 S1), while the feed is the thing an operator watches out
 * of the corner of their eye, which is exactly what a tall narrow rail is for.
 * Topology and platform health split the row below, because both are context
 * you consult rather than watch.
 *
 * It collapses to one column below `xl`. A dashboard that keeps three columns on
 * a laptop is a dashboard nobody can read.
 */
export function App() {
  return (
    <div className="flex flex-col gap-6">
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

      <KpiStrip />

      <div className="grid min-w-0 gap-5 xl:grid-cols-5">
        <DecompositionPanel className="xl:col-span-3" />
        <IncidentFeed className="xl:col-span-2" />
      </div>

      <div className="grid min-w-0 gap-5 xl:grid-cols-2">
        <CausalGraphPanel />
        <PlatformStatus />
      </div>
    </div>
  );
}
