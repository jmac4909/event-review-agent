import { test, expect } from "@playwright/test";
import { signIn } from "./login.js";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`private preview sign-in at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    const access = await (await page.request.get("/api/access")).json();
    test.skip(!access.required, "Access gate is optional for local Mock demos");
    await page.setViewportSize(viewport);
    await page.goto("/");
    const field = page.getByLabel("Preview access code", { exact: true });
    const button = page.getByRole("button", { name: "Sign in as Europe supervisor" });
    await expect(field).toHaveAttribute("type", "password");
    await expect(button).toBeDisabled();
    expect((await page.request.get("/api/events")).status()).toBe(401);
    expect((await page.request.post("/api/session", { data: { account_id: "supervisor-europe" } })).status()).toBe(401);
    await field.fill("deliberately-incorrect-test-code");
    await button.click();
    await expect(page.getByRole("alert")).toContainText("access code is incorrect");
    await field.clear();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `${screenshots}/private-login-${viewport.width}.png`, fullPage: true });
    await signIn(page, "Europe supervisor");
    await expect(page.getByRole("heading", { name: "Event feed", exact: true })).toBeVisible();
    await page.reload();
    await page.locator(".review-settings > summary").click();
    await expect(page.getByRole("form", { name: "Review model" })).toBeVisible();
    await expect(field).toHaveCount(0);
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(field).toHaveValue("");
    expect((await page.request.get("/api/feed")).status()).toBe(401);
  });
}
