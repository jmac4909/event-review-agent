import { test, expect } from "@playwright/test";
import { signIn } from "./login.js";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";

test("long lists keep selected details reachable and page through real older events", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Creates events only in the disposable database");
  test.setTimeout(90000);
  await page.goto("/");
  await signIn(page, "US supervisor");
  const source = "00000002-0000-4000-8000-000000000001";
  // Real HTTP/database pagination. No list/detail response is mocked.
  for (let index = 0; index < 52; index++) {
    const request_id = await page.evaluate(() => crypto.randomUUID());
    const response = await page.request.post(`/api/events/${source}/simulate`, { data: { request_id } });
    expect(response.ok()).toBe(true);
  }
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    await page.setViewportSize(viewport);
    await page.reload();
    const list = page.getByLabel("Regional events", { exact: true });
    await expect(list.locator(".event-row")).toHaveCount(25);
    const firstIds = await list.locator(".event-row").evaluateAll(rows => rows.map(row => row.dataset.testid));
    const last = list.locator(".event-row").last();
    const id = (await last.getAttribute("data-testid")).slice("event-".length);
    await last.click();
    await expect(page.locator(".event-id span")).toHaveText(id);
    const details = page.getByLabel("Event details", { exact: true });
    await expect(details.getByRole("heading", { level: 2 })).toBeInViewport();
    if (viewport.width > 900) {
      expect(await list.locator(".event-list").evaluate(el => el.scrollTop)).toBeGreaterThan(0);
      expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(2500);
    }
    await page.screenshot({ path: `${screenshots}/selected-old-event-${viewport.width}.png` });
    if (viewport.width <= 900) await page.getByRole("button", { name: "← Back to events" }).click();
    await list.getByRole("button", { name: "Older events", exact: true }).click();
    await expect(list.getByRole("navigation", { name: "Event pages" })).toContainText("Page 2");
    await expect.poll(async () => list.locator(".event-row").first().getAttribute("data-testid")).not.toBe(firstIds[0]);
    const secondIds = await list.locator(".event-row").evaluateAll(rows => rows.map(row => row.dataset.testid));
    expect(secondIds.some(id => firstIds.includes(id))).toBe(false);
    const secondLast = list.locator(".event-row").last();
    const secondId = (await secondLast.getAttribute("data-testid")).slice("event-".length);
    await secondLast.click();
    await expect(page.locator(".event-id span")).toHaveText(secondId);
    await expect(details.getByRole("heading", { level: 2 })).toBeInViewport();
    if (viewport.width <= 900) await page.getByRole("button", { name: "← Back to events" }).click();
    await list.getByRole("button", { name: "Newer events", exact: true }).click();
    await expect(list.locator(".event-row").first()).toHaveAttribute("data-testid", firstIds[0]);
    await list.getByLabel("Search events").fill("us-fixture-1");
    await expect(list.locator(".event-row")).toHaveCount(1);
    await expect(list.getByRole("navigation", { name: "Event pages" })).toHaveCount(0);
    await page.getByTestId(`event-${source}`).click();
    await expect(details.getByRole("heading", { level: 2 })).toBeInViewport();
    await expect(details).toContainText("Why this decision?");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(errors).toEqual([]);
});
