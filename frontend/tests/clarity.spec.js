import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const proximity = "00000002-0000-4000-8000-000000000001";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`safety feed and mobile back navigation preserve the current draft at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto("/");
    await signIn(page, "US supervisor");
    const row = page.getByTestId(`event-${proximity}`);
    await expect(row).toContainText("Safety event");
    await expect(row).toContainText("high severity");
    await expect(row).toContainText("Mock");
    await expect(page.locator(".review-settings")).not.toHaveAttribute("open");
    await expect(page.getByText("New reviews: Mock", { exact: true })).toBeVisible();
    if (viewport.width === 390) {
      const first = await page.locator(".event-row").first().boundingBox();
      expect(first.y).toBeLessThan(650);
    }
    await expect(page.getByText("Start here", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Synthetic data", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Demo data", { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `${screenshots}/clear-feed-${viewport.width}.png`, fullPage: false });
    await page.getByLabel("Search events").fill("US-CAM-04");
    await row.click();
    await expect(page.locator(".verdict")).toContainText("Safety event");
    await expect(page.locator(".verdict")).toContainText("high severity");
    // An actual high-severity result must use the warm/red palette, not green.
    const color = await page.locator(".verdict").evaluate(el => getComputedStyle(el).color);
    const [red, green] = color.match(/\d+/g).map(Number);
    expect(red).toBeGreaterThan(green);
    await page.screenshot({ path: `${screenshots}/clear-high-${viewport.width}.png`, fullPage: false });
    const form = page.getByLabel("Supervisor review", { exact: true });
    await form.getByRole("button", { name: /Correct this review|Update supervisor review/ }).click();
    await form.getByLabel("Answer or reason").fill("Unsaved review draft for navigation check.");
    if (viewport.width <= 900) {
      await expect(page.getByLabel("Regional events", { exact: true })).toBeHidden();
      await page.getByRole("button", { name: "← Back to events" }).click();
      await expect(page.getByLabel("Event details", { exact: true })).toBeHidden();
      await expect(page.getByLabel("Search events")).toHaveValue("US-CAM-04");
      await row.click();
      await expect(form.getByLabel("Answer or reason")).toHaveValue("Unsaved review draft for navigation check.");
      const nav = page.getByRole("navigation", { name: "Mobile workspace" });
      await nav.getByRole("button", { name: "Guidance", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Memory & guidance" })).toBeVisible();
      await nav.getByRole("button", { name: "Event feed", exact: true }).click();
      await row.click();
      await expect(form.getByLabel("Answer or reason")).toHaveValue("Unsaved review draft for navigation check.");
    }
    await form.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("approving dismissal guidance needs no severity and remains separate from the source correction", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to the disposable browser database");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await signIn(page, "US supervisor");
  const source = "00000002-0000-4000-8000-000000000003";
  await page.getByTestId(`event-${source}`).click();
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: "Correct this review" }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  await form.getByLabel("Answer or reason").fill("Rules for this site: blocking is an issue only over 120 seconds; dismiss this 95-second alert.");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(page.locator(".verdict")).toContainText("Alert dismissed");
  await expect(page.locator(".verdict .severity")).toHaveCount(0);
  await form.getByRole("button", { name: "Review suggested guidance" }).click();
  const card = page.locator(".guidance-card").first();
  await expect(card).toContainText("No reusable wording proposed");
  await card.getByRole("button", { name: "Write reusable guidance" }).click();
  await expect(card.getByRole("combobox")).toHaveCount(0);
  await expect(card.locator(".guidance-effect")).toContainText("Alert dismissed");
  await expect(card.locator(".guidance-effect")).toContainText("From your correction.");
  await expect(card.locator(".guidance-effect .severity")).toHaveCount(0);
  await expect(card.getByRole("combobox", { name: "Severity of the safety event", exact: true })).toHaveCount(0);
  // Mock does not understand arbitrary policy changes. A supervisor can supply
  // explicit reusable wording, and approves it separately from the correction.
  const wording = "Dismiss matching 95-second exit obstruction alerts. The supervisor's site rule requires more than 120 seconds.";
  await card.getByLabel("Reusable guidance").fill(wording);
  await card.screenshot({ path: `${screenshots}/approve-dismissal-mobile.png` });
  const approval = page.waitForRequest(request => request.url().endsWith("/approve") && request.method() === "POST");
  await card.getByRole("button", { name: "Approve guidance", exact: true }).click();
  expect((await approval).postDataJSON()).toEqual({ text: wording });
  await page.getByRole("button", { name: /^Approved/ }).click();
  await expect(page.locator(".guidance-card").first()).toContainText("Alert dismissed");
  await page.reload();
  await page.getByRole("navigation", { name: "Mobile workspace" }).getByRole("button", { name: "Guidance", exact: true }).click();
  await page.getByRole("button", { name: /^Approved/ }).click();
  await page.locator(".guidance-card").first().getByRole("button", { name: "Open source event" }).click();
  await page.getByRole("button", { name: "Simulate similar event", exact: true }).click();
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  await expect(page.locator(".verdict")).toContainText("Mock cannot interpret these guidance conditions", { timeout: 15000 });
  await expect(page.locator(".verdict > strong")).toHaveText("Needs input");
  await expect(page.locator(".verdict .severity")).toHaveCount(0);
  await page.getByText("Rules and memory supplied", { exact: true }).click();
  await expect(page.locator(".decision-evidence")).toContainText(wording);
  const saved = await (await page.request.get(`/api/events/${source}`)).json();
  expect(saved.corrections[0].decision).toBe("rejected");
  expect(saved.corrections[0].severity).toBeNull();
  expect(saved.review.result.decision).toBe("confirmed");
});

test("existing guidance conflicts show both outcomes and a human repair path", async ({ page }) => {
  // Display fixture reproducing the reported contradiction. No saved guidance
  // is changed, and neither approval nor retirement is submitted here.
  await page.route("**/api/memory", route => route.fulfill({ status: 200, contentType: "application/json",
    body: JSON.stringify({ corrections: [], guidance: [{
      id: "display-conflict", source_event_id: "00000001-0000-4000-8000-000000000003",
      site: "North warehouse", zone: "East exit", camera: "EU-CAM-02", event_type: "blocked_exit",
      status: "approved", decision: "confirmed", severity: "high", source_decision: "rejected", source_severity: null,
      proposed_text: "Blocking is an issue only over 120 seconds.",
      approved_text: "Dismiss matching 95-second detections under the 120-second site rule.",
      metadata_condition: { duration_seconds: 95 }, draft_status: "not_requested", draft_result: null, draft_history: [],
    }] }) }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await page.getByRole("navigation", { name: "Mobile workspace" }).getByRole("button", { name: "Guidance", exact: true }).click();
  await page.getByRole("button", { name: /^Approved/ }).click();
  const card = page.getByTestId("guidance-display-conflict");
  await expect(card.locator(".guidance-effect")).toContainText("Safety event · high severity");
  await expect(card.getByRole("note")).toContainText("Guidance and source correction differ");
  await expect(card.getByRole("note")).toContainText("Alert dismissed");
  await expect(card.getByRole("button", { name: "Stop using guidance" })).toBeVisible();
  await card.screenshot({ path: `${screenshots}/legacy-guidance-conflict-mobile.png` });
  await card.getByRole("button", { name: "Open source event" }).click();
  await expect(page.getByLabel("Event details", { exact: true })).toContainText("EU-CAM-02");
});
