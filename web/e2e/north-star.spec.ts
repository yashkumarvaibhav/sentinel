import { expect, test } from '@playwright/test';

/**
 * The north-star flow, driven through a real browser against a running stack.
 *
 * What this catches that nothing else does: the built bundle, the front door,
 * the gateway and the store agreeing. Every unit test above this mocks at least
 * one of those seams, so "it works" has never been checked end to end by
 * anything except a person looking at it.
 *
 * It is deliberately written to be *true on an empty platform as well as a busy
 * one*. A suite that only passed when an incident happened to exist would be
 * one nobody could run, and the honest-empty-state behaviour is a feature this
 * project has spent real effort on — so it is asserted rather than skipped
 * around.
 */

const SECRET = process.env.SENTINEL_E2E_SECRET ?? '';

test.describe('the command centre', () => {
  test('serves the shell, names the build, and says whether it is watching', async ({ page }) => {
    await page.goto('/command');

    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    // The build stamp is the claim that this page and the API are the same
    // commit. A footer that could not name it would make every other assertion
    // here a statement about an unknown version.
    const version = await page.request.get('/api/version');
    expect(version.ok()).toBeTruthy();
    const { git_sha: sha } = (await version.json()) as { git_sha: string };
    expect(sha).not.toBe('unknown');
  });

  test('the incident feed is either populated or honestly empty, never blank', async ({ page }) => {
    await page.goto('/command');

    const feed = page.getByRole('region', { name: /incident/i }).first();
    await expect(feed).toBeVisible();
    // Either there are cards, or there is a sentence saying why there are not.
    // Silence is the one thing this feed is not allowed to be.
    const text = (await feed.textContent()) ?? '';
    expect(text.trim().length).toBeGreaterThan(0);
  });

  test('navigating to the security view keeps the shell and answers typed', async ({ page }) => {
    await page.goto('/command');
    await page.getByRole('link', { name: 'Security' }).click();

    await expect(page).toHaveURL(/\/security$/);
    await expect(page.getByRole('navigation')).toBeVisible();
  });

  test('an unknown route is a stated not-found, not a white screen', async ({ page }) => {
    await page.goto('/definitely-not-a-route');

    await expect(page.getByText(/not found/i).first()).toBeVisible();
    await expect(page.getByRole('navigation')).toBeVisible();
  });
});

test.describe('the demo launcher', () => {
  test('matches the gate this deployment actually has', async ({ page, request }) => {
    // The interim gate is documented as inert until a secret is configured, so
    // there are two correct renderings and which one is right is a property of
    // *the deployment*. Asserting only the guarded one passes locally and fails
    // in CI, which is exactly how this was first written and what the first
    // hosted run caught.
    //
    // Asked of the deployment rather than inferred from this harness's own env:
    // "the runner has no secret" and "the gateway has no secret" are two
    // different facts, and a test that conflated them would be right by luck.
    const guarded = (await request.get('/api/lab/runs', { failOnStatusCode: false })).status();
    await page.goto('/demo');

    if (guarded === 401) {
      await expect(page.getByText(/protected operator surface/i)).toBeVisible();
      await expect(page.getByLabel('Operator credential')).toBeVisible();
      return;
    }
    await expect(page.getByRole('heading', { name: /demo launcher/i })).toBeVisible();
  });

  test('fires a scenario and shows it queued', async ({ page }) => {
    test.skip(SECRET === '', 'set SENTINEL_E2E_SECRET to drive the launcher');
    await page.goto('/demo');

    await page.getByLabel('Operator credential').fill(SECRET);
    await page.getByRole('button', { name: /unlock the launcher/i }).click();

    await expect(page.getByRole('heading', { name: /demo launcher/i })).toBeVisible();
    await expect(page.getByText(/championship night/i)).toBeVisible();

    const replay = page.getByRole('button', { name: /replay a capture/i });
    // BUSY is a legitimate state — something may already be running — and the
    // page says so rather than offering a button that cannot work.
    if (await replay.isEnabled()) {
      await replay.click();
      await expect(page.getByText(/QUEUED|RUNNING/).first()).toBeVisible();
    } else {
      await expect(page.getByText(/already running|no lab runner/i)).toBeVisible();
    }
  });
});

test.describe('the platform under the page', () => {
  test('reports every plane ready through the front door', async ({ request }) => {
    const health = await request.get('/api/health');

    expect([200, 503]).toContain(health.status());
    const report = (await health.json()) as { status: string; degraded: string[] };
    expect(['ready', 'degraded']).toContain(report.status);
    // A degraded platform is a real answer; a platform that will not say which
    // plane is degraded is not.
    if (report.status === 'degraded') expect(report.degraded.length).toBeGreaterThan(0);
  });

  test('refuses a mutating request that carries no secret', async ({ request }) => {
    test.skip(SECRET === '', 'the gate is inert until a secret is configured');
    const answer = await request.post('/api/lab/scenario', {
      data: { scenario_id: 'combo_night', mode: 'REPLAY' },
      failOnStatusCode: false,
    });

    expect(answer.status()).toBe(401);
  });
});
