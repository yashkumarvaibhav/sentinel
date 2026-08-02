import { DemoLauncher } from '@/components/DemoLauncher';
import { InfoPopover } from '@/ui/InfoPopover';

/** The `/demo` route: fire a scenario, and watch the platform catch it. */
export function DemoPage() {
  return (
    <div className="flex flex-col gap-6">
      {/* This screen started at an `h2`, so its heading outline had no root and
          anyone navigating by headings was dropped into a subsection of
          nothing. */}
      <header className="flex items-center gap-2">
        <h1 className="font-serif">Demo launcher</h1>
        <InfoPopover label="What firing a scenario does">
          A <strong>replay</strong> re-runs a recorded capture — instant and bit-exact, the
          reliable path. A <strong>live</strong> run drives the real testbed for real minutes and
          is only statistically reproducible. Both are labelled wherever their results appear.
        </InfoPopover>
      </header>

      <DemoLauncher />
    </div>
  );
}
