import { DemoLauncher } from '@/components/DemoLauncher';

/** The `/demo` route: fire a scenario, and watch the platform catch it. */
export function DemoPage() {
  return (
    <div className="space-y-6">
      <DemoLauncher />
    </div>
  );
}
