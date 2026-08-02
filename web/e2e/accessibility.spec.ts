import { AxeBuilder } from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

/**
 * WCAG 2.1 A/AA, asserted in a real browser on both themes.
 *
 * This exists because accessibility here is a per-slice obligation rather than
 * a cleanup phase, and because the failure it first caught was invisible to
 * every other check in the repo: `text-accent` on `bg-accent-soft` — the active
 * nav pill and the verdict chip — fails contrast on the light page while
 * passing on the dark one. The house recipe uses `--accent-hover` on that fill
 * for exactly this reason. Nothing but a contrast engine run against *both*
 * themes would have found it, and a screenshot review had already missed it.
 *
 * Both themes matter independently: the tokens are AA on each ground
 * separately, so a pairing can be legal in one and illegal in the other.
 */

const ROUTES = ['/command', '/security', '/demo'] as const;
const THEMES = ['light', 'dark'] as const;

for (const theme of THEMES) {
  for (const route of ROUTES) {
    test(`${route} has no WCAG A/AA violations in the ${theme} theme`, async ({ page }) => {
      // Pinned the way the app itself pins it, before first paint, so this is
      // the same code path a user with a stored preference takes.
      await page.addInitScript((pinned) => {
        try {
          localStorage.setItem('sentinel.theme', pinned);
        } catch {
          // A browser with storage denied still renders; it just follows the OS.
        }
      }, theme);

      await page.goto(route);
      // The shell's main region, not an `h1`: `/security` and `/demo` do not
      // have one today (an information-architecture gap owned by 6.11d, and a
      // best-practice rule rather than an A/AA one, so it is not this spec's
      // business to fail on it). Waiting for content that every route actually
      // renders keeps this measuring contrast rather than routing.
      await expect(page.locator('#main')).toBeVisible();
      await page.waitForLoadState('networkidle');

      const { violations } = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
        .analyze();

      // Name the offenders in the failure message: "expected 0, got 2" sends
      // the next person back to the browser to find out which two.
      expect(
        violations.map((violation) => `${violation.id}: ${violation.help}`),
        `axe found ${violations.length} violation(s) on ${route} (${theme})`,
      ).toEqual([]);
    });
  }
}
