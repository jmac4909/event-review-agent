import { readFileSync } from "node:fs";
import { expect } from "@playwright/test";

export async function signIn(page, name) {
  const button = page.getByRole("button", { name: `Sign in as ${name}` });
  await expect(button).toBeVisible();
  const field = page.getByLabel("Preview access code", { exact: true });
  if (await field.count()) {
    // A hosted credential stays in a protected file, outside logs and Git.
    const code = process.env.REVIEW_ACCESS_CODE_FILE
      ? readFileSync(process.env.REVIEW_ACCESS_CODE_FILE, "utf8").trim()
      : process.env.REVIEW_TEST_ACCESS_CODE;
    if (!code) throw new Error("Private preview requires REVIEW_ACCESS_CODE_FILE or the local test fixture.");
    await field.fill(code);
  }
  await button.click();
  // A click can return before the login response installs the session cookie.
  // Subsequent API checks must wait for the authenticated UI, even on HTTPS.
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
}
