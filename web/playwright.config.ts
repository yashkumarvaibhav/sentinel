import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end against a *running* stack, never a mocked one.
 *
 * These specs exist to catch what the unit tests structurally cannot: that the
 * built bundle, the front door, the gateway and the store agree with each
 * other. That means they need something real to point at, which is why the
 * base URL is the compose front door rather than a dev server, and why there
 * is no `webServer` block starting one on their behalf — a suite that quietly
 * booted its own backend would be testing a different system from the one that
 * ships.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 30_000,
  use: {
    baseURL: process.env.SENTINEL_E2E_BASE_URL ?? 'http://127.0.0.1:8041',
    trace: 'retain-on-failure',
  },
  // The full Chromium build rather than Playwright's headless shell: it is the
  // engine a person actually views this in, and it is one download instead of
  // two on a machine that already has it.
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        channel: 'chromium',
        // The platform states its own reduced-motion contract, so the browser
        // checking it must actually be asking for one.
        contextOptions: { reducedMotion: 'reduce' },
      },
    },
  ],
});
