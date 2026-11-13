import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const questionId = "00000002-0000-4000-8000-000000000002";

test("a continuing regional stream recovers a lost response, produces reviews, and stays paused after reload", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Stream writes only target the disposable browser database");
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.locator(".review-settings > summary").click();
  const controls = page.getByRole("region", { name: "Demo event stream" });
  await expect(controls).toContainText("Paused");
  const initial = await (await page.request.get("/api/feed")).json();
  expect(initial.reviewer).toBe("mock");
  await page.route("**/api/feed", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Test stream outage" }) });
    } else await route.continue();
  });
  await controls.getByRole("button", { name: "Start stream", exact: true }).click();
  await expect(controls.getByRole("alert")).toContainText("Test stream outage");
  await expect(controls).toContainText("Paused");
  await page.unroute("**/api/feed");
  await page.route("**/api/feed", async (route) => {
    if (route.request().method() === "PUT") {
      const committed = await route.fetch();
      expect(committed.status()).toBe(200);
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Simulated lost acknowledgement" }) });
    } else await route.continue();
  });
  const lostAcknowledgement = page.waitForResponse((response) => response.url().endsWith("/api/feed")
    && response.request().method() === "PUT" && response.status() === 503);
  await controls.getByRole("button", { name: "Start stream", exact: true }).click();
  await lostAcknowledgement; // Finish the injected response before removing its route.
  await expect(controls).toContainText("Running");
  await expect(controls.getByRole("alert")).toHaveCount(0);
  await page.unroute("**/api/feed");
  await expect.poll(async () => (await (await page.request.get("/api/feed")).json()).generated,
    { timeout: 15000 }).toBeGreaterThanOrEqual(initial.generated + 2);
  await controls.getByRole("button", { name: /^Last arrival:/ }).click();
  await expect(page.getByLabel("Event details")).toContainText("demo-stream-");
  await expect(page.getByLabel("Review and attempts").locator(".verdict")).toBeVisible({ timeout: 15000 });
  const running = await (await page.request.get("/api/feed")).json();
  expect(running.version).toBe(initial.version + 1);
  expect(running.run_seconds).toBe(300);
  expect(running.stops_at).toBeTruthy();
  await expect(controls).toContainText("Stops at");
  await expect(controls.getByLabel("Run for")).toBeDisabled();
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
    await page.setViewportSize(viewport);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (viewport.width <= 900 && await page.getByRole("button", { name: "← Back to events" }).isVisible())
      await page.getByRole("button", { name: "← Back to events" }).click();
    await controls.screenshot({ path: `${screenshots}/stream-running-${viewport.width}.png` });
  }
  await controls.getByRole("button", { name: "Pause stream", exact: true }).click();
  await expect(controls).toContainText("Paused");
  const paused = await (await page.request.get("/api/feed")).json();
  // Observe more than two configured arrival intervals with the real workers.
  await page.waitForTimeout((paused.interval_seconds * 2 + 1) * 1000);
  expect((await (await page.request.get("/api/feed")).json()).generated).toBe(paused.generated);
  await page.reload();
  await page.locator(".review-settings > summary").click();
  await expect(controls).toContainText("Paused");
  await expect(controls).toContainText(`${paused.generated} stream events`);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, "US supervisor");
  await page.locator(".review-settings > summary").click();
  const other = await (await page.request.get("/api/feed")).json();
  expect(other.status).toBe("paused");
  expect(other.generated).toBe(0);
});

test("a supervisor draft survives when new arrivals push its event out of the loaded list", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "US supervisor");
  await page.locator(".review-settings > summary").click();
  await page.getByTestId(`event-${questionId}`).click();
  const form = page.getByRole("region", { name: "Supervisor review", exact: true });
  await form.getByRole("button", { name: /Answer and resolve|Update supervisor review/ }).click();
  await form.getByLabel("Answer or reason").fill("Keep this draft while newer events arrive.");
  // Simulate the server's latest-100 window rotating; event details still come
  // from the real database. No event or correction is changed by this check.
  await page.route("**/api/events", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, items: body.items.filter((event) => event.id !== questionId) } });
  });
  await expect(page.getByTestId(`event-${questionId}`)).toHaveCount(0);
  await expect(form.getByLabel("Answer or reason")).toHaveValue("Keep this draft while newer events arrive.");
  await form.screenshot({ path: `${screenshots}/draft-after-new-arrivals.png` });
  await form.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: /^Needs input/ }).click();
  await expect(page.getByTestId(`event-${questionId}`)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Needs supervisor input", exact: true })).toBeVisible();
});


test("an indefinite stream choice persists through reload and can be paused", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only target the disposable browser database");
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.locator(".review-settings > summary").click();
  const controls = page.getByRole("region", { name: "Demo event stream" });
  await controls.getByLabel("Run for").selectOption("none");
  await controls.getByRole("button", { name: "Start stream", exact: true }).click();
  await expect(controls).toContainText("Runs until paused.");
  let state = await (await page.request.get("/api/feed")).json();
  expect(state.stops_at).toBeNull();
  expect(state.run_seconds).toBeNull();
  await page.reload();
  await page.locator(".review-settings > summary").click();
  await expect(controls.getByLabel("Run for")).toHaveValue("none");
  await controls.getByRole("button", { name: "Pause stream", exact: true }).click();
  await expect(controls).toContainText("Paused · Mock reviews");
  state = await (await page.request.get("/api/feed")).json();
  expect(state.status).toBe("paused");
});


test("an expired stream offers a fresh start without an active countdown", async ({ page }) => {
  // Expiry itself is verified against Postgres in test_feed.py; this is its UI state.
  await page.route("**/api/feed", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, status: "auto_stopped", run_seconds: 300,
      stops_at: "2026-01-01T00:05:00Z" } });
  });
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.locator(".review-settings > summary").click();
  const controls = page.getByRole("region", { name: "Demo event stream" });
  await expect(controls).toContainText("Time limit reached · Mock reviews");
  await expect(controls.getByRole("button", { name: "Start stream", exact: true })).toBeEnabled();
  await expect(controls.getByLabel("Run for")).toBeEnabled();
  await expect(controls).not.toContainText("Stops at");
  await controls.screenshot({ path: `${screenshots}/stream-auto-stopped.png` });
});
