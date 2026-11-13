import { test, expect } from "@playwright/test";
import { signIn } from "./login.js";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const eventId = "00000002-0000-4000-8000-000000000002";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`review progress uses the claimed model and stops on completion at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    let completed = false;
    const started = Date.now() - 12000;
    // Display-only provider fixture: no live model call or database mutation.
    await page.route(`**/api/events/${eventId}`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const attempt = body.attempts.find((item) => item.id === body.review.attempt_id);
      attempt.started_at = new Date(started).toISOString();
      attempt.expires_at = new Date(started + 900000).toISOString();
      attempt.reviewer_mode = "codex";
      attempt.model_name = completed ? "codex/display-fixture" : null;
      attempt.status = completed ? "completed" : "running";
      attempt.finished_at = completed ? new Date(started + 5600).toISOString() : null;
      attempt.steps = [{ kind: "model", usage: { provider: "codex", input_tokens: 12417,
        cached_input_tokens: 7808, output_tokens: 79 } }];
      body.attempts = [attempt];
      body.corrections = [];
      body.status = completed ? "needs_input" : "claimed";
      if (!completed) body.review = null;
      await route.fulfill({ response, json: body });
    });
    await page.goto("/");
    await signIn(page, "US supervisor");
    await expect(page.locator(".review-settings > summary")).toContainText("New reviews: Mock");
    await page.getByTestId(`event-${eventId}`).click();
    const panel = page.getByLabel("Review and attempts");
    const progress = panel.locator(".review-waiting");
    await expect(progress).toHaveText(/Codex reviewing · \d+s/);
    const first = await progress.textContent();
    await expect(progress).not.toHaveText(first);
    await panel.screenshot({ path: `${screenshots}/review-progress-${viewport.width}.png` });
    completed = true;
    await expect(progress).toHaveCount(0);
    await expect(panel.locator(".review-metrics")).toHaveText("Agent review: 5.6s · 12,417 input / 79 output tokens");
    await expect(panel.locator(".verdict")).toContainText("Needs input");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await panel.screenshot({ path: `${screenshots}/review-completed-${viewport.width}.png` });
    await panel.getByRole("button", { name: /Answer and resolve|Update supervisor review/ }).click();
    await expect(panel.getByLabel("Answer or reason")).toBeEditable();
    await panel.getByLabel("Answer or reason").fill("Unsaved progress check");
  });
}
