import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const eu = "00000001-0000-4000-8000-000000000003";
const usQuestion = "00000002-0000-4000-8000-000000000002";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`supervisor input is reachable and drafts survive refresh at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/");
    await signIn(page, "US supervisor");
    await page.getByTestId(`event-${usQuestion}`).click();
    const panel = page.getByLabel("Supervisor review", { exact: true });
    await panel.getByRole("button", { name: /Answer and resolve|Update supervisor review/ }).click();
    await panel.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
    await panel.getByLabel("Answer or reason").fill("Draft only: authorization needs to be checked for this person.");
    await page.waitForResponse((response) => response.url().endsWith(`/api/events/${usQuestion}`) && response.ok());
    await expect(panel.getByLabel("Answer or reason")).toHaveValue("Draft only: authorization needs to be checked for this person.");
    await page.screenshot({ path: `${screenshots}/supervisor-form-${viewport.width}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.getByRole("button", { name: "Cancel", exact: true }).click();
    const nav = viewport.width > 1000 ? page.locator(".sidebar") : page.getByRole("navigation", { name: "Mobile workspace" });
    await nav.getByRole("button", { name: viewport.width > 1000 ? "Memory & guidance" : "Guidance", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Memory & guidance", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Suggested/ })).toBeVisible();
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Choose a region" })).toBeVisible();
  });
}

test("supervisor resolves a question despite an initial outage and a lost save acknowledgement", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to this slice's disposable browser database");
  await page.goto("/");
  await signIn(page, "US supervisor");
  await page.getByTestId(`event-${usQuestion}`).click();
  const panel = page.getByLabel("Supervisor review", { exact: true });
  await panel.getByRole("button", { name: "Answer and resolve" }).click();
  await panel.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  const reason = "This person had the permit for this particular maintenance visit.";
  await panel.getByLabel("Answer or reason").fill(reason);
  await page.route(`**/api/events/${usQuestion}`, (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Test refresh outage" }) }));
  await expect(page.getByRole("alert")).toContainText("Draft kept.");
  await expect(panel.getByLabel("Answer or reason")).toHaveValue(reason);
  await page.unroute(`**/api/events/${usQuestion}`);
  await page.route(`**/api/events/${usQuestion}/corrections`, (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Test save outage" }) }));
  await panel.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("Test save outage");
  await expect(panel.getByLabel("Answer or reason")).toHaveValue(reason);
  await page.unroute(`**/api/events/${usQuestion}/corrections`);
  await page.route(`**/api/events/${usQuestion}/corrections`, async (route) => {
    const committed = await route.fetch();
    expect(committed.status()).toBe(200);
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Simulated lost acknowledgement" }) });
  });
  await panel.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(page.locator(".verdict")).toContainText("Alert dismissed", { timeout: 10000 });
  await expect(panel.getByLabel("Answer or reason")).toHaveCount(0);
  await page.reload();
  await page.getByTestId(`event-${usQuestion}`).click();
  await expect(page.locator(".verdict")).toContainText(reason);
  const saved = await (await page.request.get(`/api/events/${usQuestion}`)).json();
  expect(saved.status).toBe("reviewed");
  expect(saved.corrections).toHaveLength(1);
  expect(saved.review.result.decision).toBe("needs_input");
  await page.screenshot({ path: `${screenshots}/supervisor-answer-saved.png`, fullPage: true });
});

test("a severity correction requires approval before guidance changes a later mock verdict", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to this slice's disposable browser database");
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await page.getByTestId(`event-${eu}`).click();
  await expect(page.getByLabel("Review and attempts")).toContainText("Safety event");
  await expect(page.getByLabel("Review and attempts")).toContainText("medium severity");
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: "Correct this review" }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("confirmed");
  await form.getByRole("combobox", { name: "Severity", exact: true }).selectOption("high");
  await form.getByLabel("Answer or reason").fill("Site policy: Matching 95-second pallet obstructions at East exit are high severity. This is a standing rule.");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(page.locator(".verdict")).toContainText("Safety event");
  await expect(page.locator(".verdict")).toContainText("high severity");
  await form.getByRole("button", { name: "Review suggested guidance" }).click();
  const suggested = page.locator(".guidance-card").first();
  await expect(suggested).toContainText("Pending approval");
  await expect(suggested).toContainText("Suggested wording", { timeout: 15000 });
  await suggested.getByRole("button", { name: "Review & approve" }).click();
  await suggested.getByLabel("Reusable guidance").fill("Site policy: Matching 95-second pallet obstructions at East exit are high severity. This is a standing rule for future matching events.");
  await expect(suggested.getByRole("combobox")).toHaveCount(0);
  await expect(suggested.locator(".guidance-effect")).toContainText("Safety event · high severity");
  await expect(suggested.locator(".guidance-effect")).toContainText("From your correction.");
  await suggested.getByRole("button", { name: "Approve guidance" }).click();
  await expect(page.getByText("No suggested guidance yet.")).toBeVisible();
  await page.getByRole("button", { name: /^Approved/ }).click();
  const approved = page.locator(".guidance-card").first();
  await expect(approved).toContainText("high severity. This is a standing rule for future matching events.");
  await page.screenshot({ path: `${screenshots}/approved-guidance.png`, fullPage: true });
  await approved.getByRole("button", { name: "Open source event" }).click();
  await page.getByRole("button", { name: "Simulate similar event", exact: true }).click();
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  await expect(page.getByLabel("Event details").getByRole("heading", { level: 2 })).toContainText("follow-up");
  await expect(page.getByLabel("Review and attempts")).toContainText("Approved guidance:", { timeout: 15000 });
  await expect(page.getByLabel("Review and attempts").locator(".verdict > strong")).toHaveText("Safety event");
  await expect(page.getByLabel("Review and attempts")).toContainText("high severity");
  const original = await (await page.request.get(`/api/events/${eu}`)).json();
  expect(original.review.result.severity).toBe("medium");
  await page.reload();
  await page.locator(".event-row").first().click();
  await expect(page.getByLabel("Review and attempts")).toContainText("Approved guidance:");
  await page.locator(".history-panel > summary").click();
  await page.getByLabel("Review and attempts").locator(".attempt > summary").click();
  await page.getByText("Memory selection", { exact: true }).click();
  const memoryTrace = page.locator(".memory-trace");
  await expect(memoryTrace).toContainText("Word overlap + local embeddings");
  await expect(memoryTrace).toContainText("minishlab/potion-base-8M");
  await expect(memoryTrace).toContainText("Approved guidance");
  await expect(memoryTrace).toContainText("Scores rank similarity");
  await page.getByText("Context & retrieval audit", { exact: true }).click();
  await expect(page.getByLabel("Review and attempts").locator(".trace pre").first()).toContainText('"approved_guidance"');
  await page.screenshot({ path: `${screenshots}/feedback-changed-review.png`, fullPage: true });
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    await page.setViewportSize(viewport);
    await expect(memoryTrace).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await memoryTrace.screenshot({ path: `${screenshots}/memory-selection-${viewport.width}.png` });
  }
});

test("a newer supervisor answer preserves a stale draft until explicitly reloaded", async ({ page, context }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to this slice's disposable browser database");
  await page.goto("/");
  await signIn(page, "US supervisor");
  await page.getByTestId(`event-${usQuestion}`).click();
  const first = page.getByRole("region", { name: "Supervisor review", exact: true });
  await first.getByRole("button", { name: "Update supervisor review" }).click();
  await first.getByLabel("Answer or reason").fill("Unsaved draft in the first tab.");

  const other = await context.newPage();
  await other.goto("/");
  await other.getByTestId(`event-${usQuestion}`).click();
  const second = other.getByRole("region", { name: "Supervisor review", exact: true });
  await second.getByRole("button", { name: "Update supervisor review" }).click();
  await second.getByLabel("Answer or reason").fill("Updated evidence saved from the second tab.");
  await second.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(second.getByRole("status")).toContainText("saved");

  await expect(first.getByRole("alert")).toContainText("Newer review available");
  await expect(first.getByLabel("Answer or reason")).toHaveValue("Unsaved draft in the first tab.");
  await expect(first.getByRole("button", { name: "Save supervisor review", exact: true })).toBeDisabled();
  await page.screenshot({ path: `${screenshots}/stale-supervisor-draft.png`, fullPage: true });
  await first.getByRole("button", { name: "Load latest review" }).click();
  await expect(first.getByLabel("Answer or reason")).toHaveValue("Updated evidence saved from the second tab.");
  await expect(first.getByRole("button", { name: "Save supervisor review", exact: true })).toBeEnabled();
  await first.getByRole("button", { name: "Cancel", exact: true }).click();
  await other.close();
});
