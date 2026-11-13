import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const eventId = "00000002-0000-4000-8000-000000000002";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`reported usage, unknown usage, and old traces are distinct at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    // Display fixtures only: this exercises the real UI with reported metadata
    // but makes no model call and does not change any stored attempt.
    await page.route(`**/api/events/${eventId}`, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const base = { kind: "model", name: "display-fixture", input: {}, output: {}, error: null,
        started_at: "2026-11-11T09:00:00Z", finished_at: "2026-11-11T09:00:01Z" };
      body.attempts[0].steps = [
        { ...base, name: "anthropic/display-fixture", usage: { provider: "anthropic", input_tokens: 100,
          output_tokens: 20, cache_read_input_tokens: 300, cache_creation_input_tokens: 40 } },
        { ...base, name: "codex/display-fixture", usage: { provider: "codex", input_tokens: 500,
          output_tokens: 60, cached_input_tokens: 400, reasoning_output_tokens: 30 } },
        { ...base, name: "unknown-usage", error: "Model call timed out.", usage: null },
        { ...base, name: "older-call" },
      ];
      await route.fulfill({ response, json: body });
    });
    await page.goto("/");
    await signIn(page, "US supervisor");
    await page.getByTestId(`event-${eventId}`).click();
    const panel = page.getByLabel("Review and attempts");
    await panel.locator(".history-panel > summary").click();
    await panel.locator(".attempt > summary").first().click();
    await panel.getByText("Investigation steps (4)", { exact: true }).click();
    for (const [number, name] of [[1, "anthropic/display-fixture"], [2, "codex/display-fixture"], [3, "unknown-usage"], [4, "older-call"]]) {
      await panel.getByText(`${number}. Model: ${name}`, { exact: true }).click();
    }
    const usage = panel.getByRole("region", { name: "Model token usage" });
    await expect(usage).toHaveCount(2);
    await expect(usage.nth(0)).toContainText("Uncached input tokens");
    await expect(usage.nth(0)).toContainText("300");
    await expect(usage.nth(1)).toContainText("400");
    await expect(usage.nth(1)).toContainText("Cached portion of input");
    await expect(panel).not.toContainText(/subscription balance|your provider account/);
    await expect(panel).toContainText("No token counts reported for this call.");
    await expect(panel).toContainText("Token usage was not recorded for this older call.");
    await expect(panel).not.toContainText("Mock call · no model tokens used.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await usage.nth(0).screenshot({ path: `${screenshots}/anthropic-usage-${viewport.width}.png` });
    await usage.nth(1).screenshot({ path: `${screenshots}/codex-usage-${viewport.width}.png` });
  });
}
