import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";

test("compact legacy Mock wording preserves exact history and other authors' text", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "US supervisor");
  const id = "00000002-0000-4000-8000-000000000001";
  const stored = await (await page.request.get(`/api/events/${id}`)).json();
  const legacy = "Mock rule: reported separation is 1.6 m; the sample threshold is 2 m. No barrier evidence is supplied.";
  let rationale = legacy;
  let modelName = "deterministic-mock-v1";
  let human = false;
  // Display fixture only: exercise older saved wording without editing records.
  await page.route(`**/api/events/${id}`, async (route) => {
    const response = await route.fetch();
    const event = await response.json();
    event.review.result.rationale = rationale;
    const attempt = event.attempts.find(item => item.id === event.review.attempt_id);
    attempt.model_name = modelName;
    attempt.model_output = { ...event.review.result };
    attempt.steps = []; // Older reviews predate per-call tracing.
    event.corrections = human ? [{ id: "display-only", version: 1, decision: "confirmed", severity: "high",
      reason: legacy, supervisor_id: "supervisor-us", created_at: event.review.created_at }] : [];
    await route.fulfill({ response, json: event });
  });
  await page.getByTestId(`event-${id}`).click();
  await expect(page.locator(".verdict > p").first()).toHaveText("1.6 m separation; 2 m limit.");
  await expect(page.locator(".simulation-section")).toHaveText("Simulate similar event");
  await page.locator(".history-panel > summary").click();
  await expect(page.locator(".attempt > summary").first()).toContainText("Mock");
  await page.locator(".attempt > summary").first().click();
  await page.getByText("View model output", { exact: true }).click();
  await expect(page.locator(".attempt pre").last()).toContainText(legacy);

  // The same words from a real provider or a supervisor stay verbatim.
  modelName = "codex/cli-default";
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.locator(".verdict > p").first()).toHaveText(legacy);
  await expect(page.locator(".attempt > summary").first()).toContainText("Codex");
  modelName = "deterministic-mock-v7";
  rationale = `${legacy} A barrier was verified in a separate report.`;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.locator(".verdict > p").first()).toHaveText(rationale);
  human = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.locator(".verdict > p").first()).toHaveText(legacy);
  expect((await (await page.request.get(`/api/events/${id}`)).json()).review).toEqual(stored.review);
});

test("supervisor inspects a mock result and exact input, then finds a needs-input event", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await page.getByTestId("event-00000001-0000-4000-8000-000000000001").click();
  const panel = page.getByLabel("Review and attempts");
  await expect(panel).toContainText("Safety event");
  await expect(panel).toContainText("high severity");
  await panel.locator(".history-panel > summary").click();
  await panel.locator(".attempt > summary").first().click();
  await panel.getByText("Context & retrieval audit", { exact: true }).click();
  const context = panel.locator(".trace pre").first();
  await expect(context).toContainText("europe-fixture-1");
  await expect(context).toContainText('"region_id": "europe"');
  await expect(context).not.toContainText("US-CAM");
  // Wait for a real background refresh; it must not collapse the open trace.
  await page.waitForResponse((response) => response.url().endsWith("/api/events/00000001-0000-4000-8000-000000000001") && response.ok());
  await expect(context).toBeVisible();
  await page.screenshot({ path: `${screenshots}/review-context-desktop.png`, fullPage: true });
  await page.reload();
  await page.getByTestId("event-00000001-0000-4000-8000-000000000001").click();
  await expect(panel).toContainText("Safety event");
  const question = await (await page.request.get("/api/events/00000001-0000-4000-8000-000000000002")).json();
  if (question.status === "needs_input")
    await page.locator(".sidebar").getByRole("button", { name: /^Needs input/ }).click();
  await page.getByTestId("event-00000001-0000-4000-8000-000000000002").click();
  await expect(panel).toContainText("Was this person authorized");
  await expect(panel.getByRole("button", { name: question.corrections.length ? "Update supervisor review" : "Answer and resolve" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Retry failed event" })).toHaveCount(0);
  await page.screenshot({ path: `${screenshots}/needs-input-desktop.png`, fullPage: true });
  expect(errors).toEqual([]);
});

test("failed event retry reports an outage then persists a successful new review", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_RETRY !== "1", "Uses a deliberately failed event in a disposable local browser database");
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await page.getByTestId("event-00000001-0000-4000-8000-000000000004").click();
  const panel = page.getByLabel("Review and attempts");
  const retry = panel.getByRole("button", { name: "Retry failed event" });
  await expect(retry).toBeVisible();
  await page.route("**/api/events/*/retry", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Temporary retry outage." }),
  }));
  await retry.click();
  await expect(panel.getByRole("alert")).toContainText("Temporary retry outage");
  await page.unroute("**/api/events/*/retry");
  await retry.click();
  await expect(panel).toContainText("Safety event", { timeout: 15000 });
  await expect(panel.locator(".attempt")).toHaveCount(4);
  await page.reload();
  await page.getByTestId("event-00000001-0000-4000-8000-000000000004").click();
  await expect(panel).toContainText("Safety event");
  await expect(panel.locator(".attempt")).toHaveCount(4);
  await page.screenshot({ path: `${screenshots}/retry-completed-desktop.png`, fullPage: true });
});

test("supervisor sees site rules before the first model call without a mandatory lookup", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Needs a fresh local run with upfront rules");
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await page.getByTestId("event-00000001-0000-4000-8000-000000000001").click();
  const panel = page.getByLabel("Review and attempts");
  await expect(panel).toContainText("Safety event");
  await panel.locator(".history-panel > summary").click();
  await panel.locator(".attempt > summary").first().click();
  await panel.getByText("Investigation steps (1)", { exact: true }).click();
  await panel.getByText("1. Model: deterministic-mock-v7", { exact: true }).click();
  const model = panel.locator("details").filter({ has: page.locator("summary", { hasText: /^1\. Model:/ }) }).last();
  await expect(model).toContainText('"version": "review-context-v10"');
  await expect(model).toContainText('"site_rules":');
  await expect(model).toContainText('"lookups_remaining": 3');
  await expect(model).toContainText('"tool_results": []');
  await expect(model).toContainText("europe-synthetic-site-rules-v1");
  await expect(model).not.toContainText("us-synthetic-site-rules");
  await expect(model).toContainText("Mock call · no model tokens used.");
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    await page.setViewportSize(viewport);
    await model.scrollIntoViewIfNeeded();
    await expect(model.locator("pre").first()).toBeVisible();
    // Scroll the JSON viewer to the policy block for the visual evidence.
    await model.locator("pre").first().evaluate((pre) => {
      const text = pre.firstChild;
      const start = text.textContent.indexOf('"site_rules":');
      const range = document.createRange();
      range.setStart(text, start);
      range.setEnd(text, start + 12);
      pre.scrollTop += range.getBoundingClientRect().top - pre.getBoundingClientRect().top - 12;
    });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
    await page.screenshot({ path: `${screenshots}/upfront-rules-${viewport.width}.png`, fullPage: true });
  }
});
