import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";
import { signIn } from "./login.js";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const usEntry = "00000002-0000-4000-8000-000000000002";

test.beforeEach(() => test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Disposable database only"));

async function unsavedCorrection(page) {
  await page.goto("/");
  await signIn(page, "US supervisor");
  await page.getByTestId(`event-${usEntry}`).click();
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: /Answer and resolve|Update supervisor review|Correct this review/ }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  await form.getByLabel("Answer or reason").fill("Unsaved US correction; never submit this as Europe.");
  return form;
}

test("switching region in another tab clears the entire old workspace and draft", async ({ page, context }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await unsavedCorrection(page);
  const other = await context.newPage();
  await other.goto("/");
  await expect(other.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  await other.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(other, "Europe supervisor");
  await expect(page.getByRole("heading", { name: "Choose a region" })).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("session changed in another tab");
  await expect(page.locator(".workspace")).toHaveCount(0);
  await expect(page.getByLabel("Answer or reason")).toHaveCount(0);
  await page.screenshot({ path: `${screenshots}/stale-tab-cleared.png` });
  await signIn(page, "Europe supervisor");
  await expect(page.getByLabel("Search events")).toHaveValue("");
  await expect(page.locator(".event-list")).not.toContainText("US-CAM");
  await expect(page.getByLabel("Answer or reason")).toHaveCount(0);
  expect(errors).toEqual([]);
  await other.close();
});

for (const action of ["correction", "stream"]) {
  test(`the server rejects a stale ${action} even without cross-tab notification`, async ({ page }) => {
    const form = await unsavedCorrection(page);
    await page.locator(".review-settings > summary").click();
    const beforeSource = await (await page.request.get(`/api/events/${usEntry}`)).json();
    // Freeze polling after the real UI has loaded. Switching through the real
    // session API deliberately bypasses localStorage notification for this race.
    await page.route("**/api/**", route => route.request().method() === "GET" ? route.abort() : route.continue());
    const changed = await page.request.post("/api/session", { data: {
      account_id: "supervisor-europe", access_code: process.env.REVIEW_TEST_ACCESS_CODE || "",
    } });
    expect(changed.status()).toBe(200);
    const beforeFeed = await (await page.request.get("/api/feed")).json();
    const responsePromise = page.waitForResponse(response => response.request().method() === (action === "stream" ? "PUT" : "POST")
      && response.url().endsWith(action === "stream" ? "/api/feed" : `/api/events/${usEntry}/corrections`));
    if (action === "stream") await page.getByRole("button", { name: "Start stream", exact: true }).click();
    else await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
    const rejected = await responsePromise;
    expect(rejected.status()).toBe(412);
    expect(rejected.request().headers()["x-expected-supervisor"]).toBe("supervisor-us");
    await expect(page.getByRole("heading", { name: "Choose a region" })).toBeVisible();
    await expect(page.locator(".workspace")).toHaveCount(0);
    expect((await (await page.request.get("/api/feed")).json()).version).toBe(beforeFeed.version);
    await page.unroute("**/api/**");
    await signIn(page, "US supervisor");
    const after = await (await page.request.get(`/api/events/${usEntry}`)).json();
    expect(after.corrections).toEqual(beforeSource.corrections);
  });
}

test("a delayed regional response cannot replace a newly selected account", async ({ page, context }) => {
  await unsavedCorrection(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let captured;
  const ready = new Promise(resolve => { captured = resolve; });
  await page.route("**/api/events", async route => {
    const response = await route.fetch();
    expect(response.headers()["set-cookie"]).toBeUndefined();
    captured();
    await held;
    await route.fulfill({ response });
  }, { times: 1 });
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await ready;
  const other = await context.newPage();
  await other.goto("/");
  await other.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(other, "Europe supervisor");
  await expect(page.getByRole("heading", { name: "Choose a region" })).toBeVisible();
  await signIn(page, "Europe supervisor");
  release();
  await expect(page.locator(".event-list")).toContainText("EU-CAM");
  await expect(page.locator(".workspace")).not.toContainText("US-CAM");
  expect((await (await page.request.get("/api/session")).json()).region).toBe("europe");
  await other.close();
});

test("source guidance shows approval and stopped state with links to the correct card", async ({ page }) => {
  await page.goto("/");
  await signIn(page, "US supervisor");
  const created = await page.request.post("/api/events/00000002-0000-4000-8000-000000000001/simulate", {
    data: { request_id: randomUUID() },
  });
  expect(created.status()).toBe(200);
  const source = await created.json();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByTestId(`event-${source.id}`).click();
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: /Answer and resolve|Correct this review/ }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  await form.getByLabel("Answer or reason").fill("Dismiss proximity alerts only while the fixed barrier is verified intact between the routes.");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(form.getByTestId("source-guidance-status")).toHaveText("Guidance awaiting approval");
  await form.getByRole("button", { name: "Review suggested guidance" }).click();
  const card = page.locator(".guidance-card").first();
  await card.getByRole("button", { name: /Review & approve|Write reusable guidance/ }).click();
  await card.getByLabel("Reusable guidance").fill("Dismiss proximity alerts only while the fixed barrier is verified intact between the routes.");
  const id = await card.getAttribute("data-testid");
  await card.getByRole("button", { name: "Approve guidance", exact: true }).click();
  await page.getByRole("button", { name: /^Approved/ }).click();
  await page.getByTestId(id).getByRole("button", { name: "Open source event" }).click();
  await expect(form.getByTestId("source-guidance-status")).toHaveText("Guidance approved");
  await expect(form).not.toContainText("Guidance needs separate approval");
  await expect(form.getByRole("button", { name: "Review suggested guidance" })).toHaveCount(0);
  for (const width of [1440, 390, 844]) {
    await page.setViewportSize({ width, height: width === 844 ? 390 : 900 });
    await form.screenshot({ path: `${screenshots}/source-guidance-approved-${width}.png` });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await form.getByRole("button", { name: "View approved guidance" }).click();
  await expect(page.getByRole("button", { name: /^Approved/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId(id)).toBeVisible();
  await page.getByTestId(id).getByRole("button", { name: "Stop using guidance" }).click();
  await page.getByRole("button", { name: "History", exact: true }).click();
  await page.getByTestId(id).getByRole("button", { name: "Open source event" }).click();
  await expect(form.getByTestId("source-guidance-status")).toHaveText("Guidance stopped");
  await form.getByRole("button", { name: "View guidance history" }).click();
  await expect(page.getByRole("button", { name: "History", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId(id)).toBeVisible();
  await page.getByTestId(id).screenshot({ path: `${screenshots}/stopped-guidance.png` });

  await page.getByTestId(id).getByRole("button", { name: "Open source event" }).click();
  await page.getByRole("button", { name: "Simulate similar event", exact: true }).click();
  const laterResponse = page.waitForResponse(r => r.url().endsWith("/simulate") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  const later = await (await laterResponse).json();
  const detail = page.getByLabel("Event details", { exact: true });
  await expect(detail.locator(".verdict > strong")).toHaveText("Safety event", { timeout: 15000 });
  const saved = await (await page.request.get(`/api/events/${later.id}`)).json();
  expect(saved.review.result.question).toBeNull();
  expect(saved.attempts[0].context.memory.corrections).toEqual([]);
  expect(saved.attempts[0].context.memory.approved_guidance).toEqual([]);
  await detail.locator(".verdict").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${screenshots}/review-after-stopping-guidance.png` });
  await page.getByRole("button", { name: "← Back to events" }).click();
  await page.getByTestId(`event-${source.id}`).click();

  // Save another correction while a delayed detail read still describes the
  // stopped rule. Its link must open Suggested, not the previous History tab.
  const stale = await (await page.request.get(`/api/events/${source.id}`)).json();
  await page.route(`**/api/events/${source.id}`, route => route.fulfill({ json: stale }));
  await form.getByRole("button", { name: "Update supervisor review" }).click();
  await form.getByLabel("Answer or reason").fill("Dismiss only when the supervisor verifies the separating barrier before this review.");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(form.getByTestId("source-guidance-status")).toHaveCount(0);
  await form.getByRole("button", { name: "Review suggested guidance" }).click();
  await expect(page.getByRole("button", { name: /^Suggested/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".guidance-card").first()).toContainText("Pending approval");
  await page.unroute(`**/api/events/${source.id}`);
});
