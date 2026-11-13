import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`review settings show model availability and stream controls at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto("/");
    await signIn(page, "Asia supervisor");
    await page.locator(".review-settings > summary").click();
    const response = await page.request.get("/api/feed");
    expect(response.status()).toBe(200);
    const current = await response.json();
    if (current.model_selection === "regional") {
      const form = page.getByRole("form", { name: "Review model" });
      const choice = form.getByRole("combobox", { name: "Model", exact: true });
      await expect(choice).toHaveValue(current.reviewer);
      for (const provider of current.providers) {
        const option = choice.locator(`option[value="${provider.id}"]`);
        await expect(option).toContainText(provider.label);
        if (provider.enabled) await expect(option).toBeEnabled();
        else {
          await expect(option).toBeDisabled();
          await expect(option).toContainText("unavailable");
        }
      }
      await expect(form.getByRole("button", { name: "Save model" })).toBeDisabled();
      await expect(form).toContainText("Changing models pauses the stream");
    } else {
      const model = page.getByRole("region", { name: "Review model", exact: true });
      await expect(model).toContainText("Mock for all regions");
      await expect(model).toContainText("Set by the server");
      await expect(page.getByRole("combobox", { name: "Model", exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Save model" })).toHaveCount(0);
    }
    const controls = page.getByRole("region", { name: "Demo event stream" });
    await expect(controls).toContainText("Mock reviews");
    await expect(controls.getByRole("button", { name: /Start stream|Pause stream/ })).toBeEnabled();
    await expect(controls).toContainText("even with this page closed");
    await expect(controls.getByLabel("Run for")).toHaveValue(current.run_seconds === null ? "none" : String(current.run_seconds));
    await expect(controls).not.toContainText(/your.*account|subscription|Server login|Server API key/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await controls.screenshot({ path: `${screenshots}/review-settings-${viewport.width}.png` });
  });
}

test("provider selection persists, pauses arrivals, recovers lost saves and rejects stale tabs", async ({ page, context }) => {
  test.skip(process.env.REVIEW_TEST_PROVIDER_CONTROLS !== "1", "Selector writes require the disposable API fixture; workers remain Mock-only");
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.locator(".review-settings > summary").click();
  const form = page.getByRole("form", { name: "Review model" });
  const choice = form.getByRole("combobox", { name: "Model", exact: true });
  const stream = page.getByRole("region", { name: "Demo event stream" });
  await stream.getByRole("button", { name: "Start stream", exact: true }).click();
  await expect(stream).toContainText("Running · Mock reviews");

  await choice.selectOption("anthropic");
  await page.route("**/api/reviewer", (route) => route.fulfill({ status: 503,
    contentType: "application/json", body: JSON.stringify({ detail: "Test selector outage" }) }));
  await form.getByRole("button", { name: "Save model" }).click();
  await expect(stream.getByRole("alert")).toContainText("Test selector outage");
  await expect(choice).toHaveValue("anthropic");
  expect((await (await page.request.get("/api/feed")).json()).reviewer).toBe("mock");
  await page.unroute("**/api/reviewer");

  await page.route("**/api/reviewer", async (route) => {
    const saved = await route.fetch();
    expect(saved.status()).toBe(200);
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Simulated lost acknowledgement" }) });
  });
  const lostAcknowledgement = page.waitForResponse((response) => response.url().endsWith("/api/reviewer")
    && response.request().method() === "PUT" && response.status() === 503);
  await form.getByRole("button", { name: "Save model" }).click();
  await lostAcknowledgement;
  await expect(stream).toContainText("Paused · Claude reviews");
  await expect(stream.getByRole("alert")).toHaveCount(0);
  await page.unroute("**/api/reviewer");
  const saved = await (await page.request.get("/api/feed")).json();
  await page.waitForTimeout((saved.interval_seconds + 1) * 1000);
  expect((await (await page.request.get("/api/feed")).json()).generated).toBe(saved.generated);
  await page.reload();
  await page.locator(".review-settings > summary").click();
  await expect(choice).toHaveValue("anthropic");
  await form.screenshot({ path: `${screenshots}/provider-selection-saved.png` });

  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, "US supervisor");
  await page.locator(".review-settings > summary").click();
  await expect(choice).toHaveValue("mock");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await signIn(page, "Asia supervisor");
  await page.locator(".review-settings > summary").click();
  await expect(choice).toHaveValue("anthropic");
  await choice.selectOption("codex");

  const other = await context.newPage();
  await other.goto("/");
  await other.locator(".review-settings > summary").click();
  const otherForm = other.getByRole("form", { name: "Review model" });
  await otherForm.getByRole("combobox", { name: "Model", exact: true }).selectOption("mock");
  await otherForm.getByRole("button", { name: "Save model" }).click();
  await expect(other.getByRole("region", { name: "Demo event stream" })).toContainText("Paused · Mock reviews");
  await expect(form).toContainText("Settings changed. Cancel and select again.");
  await expect(choice).toHaveValue("codex");
  await form.getByRole("button", { name: "Save model" }).click();
  await expect(stream.getByRole("alert")).toContainText("controls changed");
  await form.getByRole("button", { name: "Cancel selection" }).click();
  await expect(choice).toHaveValue("mock");
  await choice.selectOption("codex");
  await form.getByRole("button", { name: "Save model" }).click();
  await expect(stream).toContainText("Paused · Codex reviews");
  await choice.selectOption("mock");
  await form.getByRole("button", { name: "Save model" }).click();
  await expect(stream).toContainText("Paused · Mock reviews");
});
