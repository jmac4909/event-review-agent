import { test, expect } from "@playwright/test";
import { signIn } from "./login.js";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const source = "00000002-0000-4000-8000-000000000001";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`event filters and summary shortcuts preserve a draft at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize(viewport);
    await page.goto("/");
    await signIn(page, "US supervisor");
    const list = page.getByLabel("Regional events", { exact: true });
    const summary = page.getByLabel("Regional event summary");
    const row = page.getByTestId(`event-${source}`);
    await expect(row).toContainText("Safety event");
    await summary.getByRole("button", { name: /^Safety events/ }).click();
    await expect(list.getByLabel("Filter events", { exact: true })).toHaveValue("safety_events");
    await list.getByLabel("Filter severity").selectOption("high");
    await list.getByLabel("Filter event type").selectOption("pedestrian_vehicle_proximity");
    await list.getByLabel("Search events").fill("us-fixture-1");
    await expect(list.locator(".event-row")).toHaveCount(1);
    await expect(list.getByRole("status")).toContainText("1 event");
    await expect(row).toContainText("high severity");
    await page.screenshot({ path: `${screenshots}/filters-safety-${viewport.width}.png`, fullPage: viewport.height < 500 });
    await row.click();
    const form = page.getByLabel("Supervisor review", { exact: true });
    await form.getByRole("button", { name: /Correct this review|Update supervisor review/ }).click();
    await form.getByLabel("Answer or reason").fill("Unsaved draft kept while filtering.");
    if (viewport.width <= 900) {
      await page.getByRole("button", { name: "← Back to events" }).click();
      await expect(list.getByLabel("Filter events", { exact: true })).toHaveValue("safety_events");
      await expect(list.getByLabel("Filter severity")).toHaveValue("high");
    }
    await list.getByLabel("Filter events", { exact: true }).selectOption("dismissed");
    await expect(list.getByLabel("Filter severity")).toHaveValue("all");
    await expect(list.getByLabel("Filter severity")).toBeDisabled();
    await expect(list.getByRole("heading", { name: "No matching events" })).toBeVisible();
    if (viewport.width > 900) {
      await expect(page.getByText("Selected event is outside this list.", { exact: true })).toBeVisible();
      await expect(form.getByLabel("Answer or reason")).toHaveValue("Unsaved draft kept while filtering.");
    }
    await list.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${screenshots}/filters-empty-${viewport.width}.png`, fullPage: viewport.height < 500 });
    await list.getByRole("button", { name: "Clear filters", exact: true }).click();
    await expect(list.getByLabel("Filter events", { exact: true })).toHaveValue("all");
    await expect(list.getByLabel("Filter severity")).toHaveValue("all");
    await expect(list.getByLabel("Filter event type")).toHaveValue("all");
    await expect(list.getByLabel("Search events")).toHaveValue("");
    await row.click();
    await expect(form.getByLabel("Answer or reason")).toHaveValue("Unsaved draft kept while filtering.");
    await form.getByRole("button", { name: "Cancel", exact: true }).click();
    if (viewport.width <= 900) await page.getByRole("button", { name: "← Back to events" }).click();
    await summary.getByRole("button", { name: /^Needs input/ }).click();
    await expect(list.getByLabel("Filter events", { exact: true })).toHaveValue("needs_input");
    await expect(row).toHaveCount(0);
    await summary.getByRole("button", { name: /^Pending review/ }).click();
    await expect(list.getByLabel("Filter events", { exact: true })).toHaveValue("pending");
    await list.getByLabel("Filter events", { exact: true }).selectOption("failed");
    await expect(list.getByRole("heading", { name: "Review failed", exact: true })).toBeVisible();
    await list.getByRole("button", { name: "Clear filters", exact: true }).click();
    await expect(row).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("a corrected alert moves from safety to dismissed without resetting filters", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to the disposable browser database");
  // Use a new fixture occurrence so other review journeys keep their own records.
  await page.goto("/");
  await signIn(page, "US supervisor");
  await page.getByTestId(`event-${source}`).click();
  const createdResponse = page.waitForResponse(response => response.url().endsWith("/simulate") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Simulate similar event", exact: true }).click();
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  const created = await (await createdResponse).json();
  await expect(page.locator(".verdict")).toContainText("Safety event", { timeout: 15000 });
  const list = page.getByLabel("Regional events", { exact: true });
  await list.getByLabel("Filter events", { exact: true }).selectOption("safety_events");
  await expect(page.getByTestId(`event-${created.id}`)).toBeVisible();
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: "Correct this review", exact: true }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  await form.getByLabel("Answer or reason").fill("This one occurrence was an incorrect detection; no reusable rule.");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(page.locator(".verdict")).toContainText("Alert dismissed");
  await expect(list.getByLabel("Filter events", { exact: true })).toHaveValue("safety_events");
  await expect(page.getByTestId(`event-${created.id}`)).toHaveCount(0);
  await list.getByLabel("Filter events", { exact: true }).selectOption("dismissed");
  await expect(page.getByTestId(`event-${created.id}`)).toContainText("Alert dismissed");
  await expect(page.getByTestId(`event-${created.id}`)).toContainText("Supervisor");
  await page.reload();
  await page.getByLabel("Filter events", { exact: true }).selectOption("dismissed");
  const saved = page.getByTestId(`event-${created.id}`);
  await expect(saved).toContainText("Alert dismissed");
  await expect(saved.locator(".severity")).toHaveCount(0);
});
