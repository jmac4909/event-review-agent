import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";

async function selectFixture(page, region, number = 1) {
  // Seed events can be older than the latest-100 list on a running demo.
  await page.getByLabel("Search events").fill(`${region}-fixture-`);
  const prefix = { europe: "00000001", us: "00000002", asia: "00000003" }[region];
  await page.getByTestId(`event-${prefix}-0000-4000-8000-${String(number).padStart(12, "0")}`).click();
}

test("supervisor can inspect events, reload, filter, and switch regions", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Choose a region" }),
  ).toBeVisible();
  await page.screenshot({
    path: `${screenshots}/login-desktop.png`,
    fullPage: true,
  });
  await signIn(page, "Europe supervisor");
  await expect(
    page.getByRole("heading", { name: "Event feed", exact: true }),
  ).toBeVisible();
  await selectFixture(page, "europe");
  expect(await page.locator(".event-row").count()).toBeGreaterThanOrEqual(4);
  await expect(page.getByLabel("Event details")).toContainText(
    "europe-fixture-1",
  );
  await page.getByText("View source metadata").click();
  await expect(page.locator(".metadata pre")).toContainText("proximity-v2");
  await page.screenshot({
    path: `${screenshots}/feed-desktop.png`,
    fullPage: true,
  });

  await page.reload();
  await selectFixture(page, "europe");
  await expect(page.getByLabel("Event details")).toContainText(
    "europe-fixture-1",
  );
  await selectFixture(page, "europe", 2);
  await expect(page.getByLabel("Event details")).toContainText(
    "europe-fixture-2",
  );
  const foreign = await page.request.get(
    "/api/events/00000002-0000-4000-8000-000000000001",
  );
  expect(foreign.status()).toBe(404);

  await page.getByLabel("Search events").fill("no such event");
  await expect(
    page.getByRole("heading", { name: "No matching events" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await page.getByLabel("Search events").fill("europe-fixture-");
  await page
    .getByLabel("Filter event type")
    .selectOption("pedestrian_vehicle_proximity");
  await expect(page.getByTestId("event-00000001-0000-4000-8000-000000000001")).toBeVisible();
  await expect(page.getByTestId("event-00000001-0000-4000-8000-000000000004")).toBeVisible();
  await expect(page.locator(".event-list")).not.toContainText("Access alert");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("heading", { name: "Choose a region" }),
  ).toBeVisible();
  expect((await page.request.get("/api/events")).status()).toBe(401);

  await signIn(page, "US supervisor");
  await selectFixture(page, "us");
  expect(await page.locator(".event-row").count()).toBeGreaterThanOrEqual(4);
  await expect(page.getByLabel("Event details")).toContainText("us-fixture-1");
  await expect(page.locator(".event-list")).not.toContainText("EU-CAM");
  expect(errors).toEqual([]);
});

test("event load failure is visible and can be retried", async ({ page }) => {
  let fail = true;
  await page.route("**/api/events", async (route) => {
    if (fail)
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Temporary test outage." }),
      });
    else await route.continue();
  });
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await expect(
    page.getByRole("heading", { name: "Events unavailable" }),
  ).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await selectFixture(page, "asia");
  expect(await page.locator(".event-row").count()).toBeGreaterThanOrEqual(4);
  await expect(page.getByLabel("Event details")).toContainText(
    "asia-fixture-1",
  );
});

test("an expired session clears regional drafts before another account signs in", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await selectFixture(page, "europe", 2);
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: /Answer and resolve|Correct this review|Update supervisor review/ }).click();
  await form.getByLabel("Answer or reason").fill("Unsaved Europe-only answer.");
  await page.getByLabel("Search events").fill("EU-CAM");

  // Invalidate the real session. The next poll must unmount its workspace.
  expect((await page.request.delete("/api/session")).status()).toBe(204);
  await expect(page.getByRole("heading", { name: "Choose a region" })).toBeVisible({ timeout: 10000 });
  await expect(page.locator(".workspace")).toHaveCount(0);
  await signIn(page, "US supervisor");
  await expect(page.getByLabel("Search events")).toHaveValue("");
  await selectFixture(page, "us", 2);
  await expect(page.getByLabel("Event details", { exact: true })).toContainText("us-fixture-2");
  await expect(page.locator(".workspace")).not.toContainText("EU-CAM");
  await form.getByRole("button", { name: /Answer and resolve|Correct this review|Update supervisor review/ }).click();
  await expect(page.getByLabel("Answer or reason")).not.toHaveValue("Unsaved Europe-only answer.");
});

test("HTTPS deployment uses a secure host-only session and rejects a foreign origin", async ({
  page,
  baseURL,
}) => {
  test.skip(
    !baseURL?.startsWith("https://"),
    "Requires the actual HTTPS deployment",
  );
  await page.goto("/");
  await signIn(page, "Europe supervisor");
  await selectFixture(page, "europe");
  const cookie = (await page.context().cookies()).find(
    (item) => item.name === "event_review_session",
  );
  // Assert booleans so a failed check cannot print the session's value.
  expect(Boolean(cookie?.secure && cookie?.httpOnly)).toBe(true);
  expect(cookie?.domain === new URL(baseURL).hostname).toBe(true);
  const rejected = await page.request.post("/api/session", {
    headers: { Origin: "https://unrelated.example" },
    data: { account_id: "supervisor-us" },
  });
  expect(rejected.status()).toBe(403);
  expect((await (await page.request.get("/api/session")).json()).region).toBe(
    "europe",
  );
});
