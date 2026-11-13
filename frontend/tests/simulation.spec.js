import { test, expect } from "@playwright/test";
import { signIn } from "./login.js";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const proximity = "00000003-0000-4000-8000-000000000001";
const access = "00000003-0000-4000-8000-000000000002";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`explicit date and distance scenarios use the normal review pipeline at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    test.setTimeout(60000); // Five real worker reviews plus polling in this journey.
    test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to the disposable browser database");
    await page.setViewportSize(viewport);
    await page.goto("/");
    await signIn(page, "Asia supervisor");
    await page.getByLabel("Search events").fill("asia-fixture-2");
    await page.getByTestId(`event-${access}`).click();
    const detail = page.getByLabel("Event details", { exact: true });
    await expect(detail).toContainText("Access alert");
    const simulate = page.getByLabel("Simulate event", { exact: true });
    let source = await (await page.request.get(`/api/events/${access}`)).json();
    for (const [scenario, days] of [["same_day", 0], ["two_days_later", 2]]) {
      await simulate.getByRole("button", { name: "Simulate similar event", exact: true }).click();
      await simulate.getByLabel("Test scenario").selectOption(scenario);
      await expect(simulate.locator("option")).toHaveCount(3);
      const response = page.waitForResponse(r => r.url().endsWith("/simulate") && r.request().method() === "POST");
      await simulate.getByRole("button", { name: "Create event", exact: true }).click();
      const created = await (await response).json();
      expect(Date.parse(created.occurred_at)).toBe(Date.parse(source.occurred_at) + days * 86400000);
      expect(created.simulation_scenario).toBe(scenario);
      await expect(detail.locator(".event-id")).toContainText(created.id);
      await expect(detail.locator(".verdict > strong")).toHaveText("Needs input", { timeout: 15000 });
      const saved = await (await page.request.get(`/api/events/${created.id}`)).json();
      expect(saved.attempts[0].context.event.occurred_at).toBe(created.occurred_at);
      await page.screenshot({ path: `${screenshots}/date-${scenario}-${viewport.width}.png`, fullPage: true });
      source = created;
    }
    if (viewport.width <= 900) await page.getByRole("button", { name: "← Back to events" }).click();
    await page.getByLabel("Search events").fill("asia-fixture-1");
    await page.getByTestId(`event-${proximity}`).click();
    await expect(detail).toContainText("Vehicle proximity");
    for (const [scenario, distance] of [["distance_3m", 3], ["distance_4_99m", 4.99], ["distance_5m", 5]]) {
      await simulate.getByRole("button", { name: "Simulate similar event", exact: true }).click();
      await simulate.getByLabel("Test scenario").selectOption(scenario);
      await expect(simulate.locator("option")).toHaveCount(4);
      // Polling must not reset the selected evidence.
      await page.waitForResponse(r => /\/api\/events\/[^/?]+$/.test(r.url()) && r.ok());
      await expect(simulate.getByLabel("Test scenario")).toHaveValue(scenario);
      await simulate.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${screenshots}/distance-chooser-${distance}-${viewport.width}.png`, fullPage: false });
      const response = page.waitForResponse(r => r.url().endsWith("/simulate") && r.request().method() === "POST");
      await simulate.getByRole("button", { name: "Create event", exact: true }).click();
      const created = await (await response).json();
      expect(created.metadata.estimated_distance_m).toBe(distance);
      await expect(detail.locator(".event-id")).toContainText(created.id);
      // Mock follows the original 2m baseline. No guidance is injected here.
      await expect(detail.locator(".verdict > strong")).toHaveText("Alert dismissed", { timeout: 15000 });
      await expect(detail.locator(".simulation-label")).toHaveText(`Distance test · ${distance} m`);
      const saved = await (await page.request.get(`/api/events/${created.id}`)).json();
      expect(saved.attempts[0].context.event.metadata.estimated_distance_m).toBe(distance);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("simulation retries a lost acknowledgement without duplicating or resetting the event", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to the disposable browser database");
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.getByLabel("Search events").fill("asia-fixture-1");
  await page.getByTestId(`event-${proximity}`).click();
  const before = await (await page.request.get("/api/events")).json();
  const original = await (await page.request.get(`/api/events/${proximity}`)).json();
  await page.getByRole("button", { name: "Simulate similar event", exact: true }).click();
  await page.getByLabel("Test scenario").selectOption("distance_3m");
  let request;
  await page.route(`**/api/events/${proximity}/simulate`, async route => {
    request = route.request().postDataJSON();
    const saved = await route.fetch();
    expect(saved.status()).toBe(200);
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Simulated lost response" }) });
  });
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  await expect(page.getByLabel("Simulate event").getByRole("alert")).toContainText("Simulated lost response");
  await expect(page.getByLabel("Test scenario")).toHaveValue("distance_3m");
  await page.unroute(`**/api/events/${proximity}/simulate`);
  const replay = page.waitForRequest(r => r.url().endsWith("/simulate") && r.method() === "POST");
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  expect((await replay).postDataJSON()).toEqual(request);
  await expect(page.getByLabel("Event details").locator(".event-id")).toContainText(request.request_id);
  await expect(page.locator(".verdict > strong")).toHaveText("Alert dismissed", { timeout: 15000 });
  expect((await (await page.request.get("/api/events")).json()).counts.total).toBe(before.counts.total + 1);
  expect((await (await page.request.get(`/api/events/${proximity}`)).json()).review).toEqual(original.review);
  await page.reload();
  await page.getByLabel("Search events").fill(`simulation-${request.request_id}`);
  await page.getByTestId(`event-${request.request_id}`).click();
  await expect(page.getByLabel("Event details").locator(".simulation-label")).toHaveText("Distance test · 3 m");
});
