import { signIn } from "./login.js";
import { test, expect } from "@playwright/test";

const screenshots = process.env.REVIEW_SCREENSHOT_DIR || "../.work/evidence";
const sourceId = "00000003-0000-4000-8000-000000000003";

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 844, height: 390 }]) {
  test(`guidance draft is inspectable, preserves edits, and needs approval at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to a disposable database");
    await page.setViewportSize(viewport);
    await page.goto("/");
    await signIn(page, "Asia supervisor");
    await page.getByTestId(`event-${sourceId}`).click();
    const form = page.getByLabel("Supervisor review", { exact: true });
    await form.getByRole("button", { name: /Correct this review|Update supervisor review/ }).click();
    await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("confirmed");
    await form.getByRole("combobox", { name: "Severity", exact: true }).selectOption("high");
    await form.getByLabel("Answer or reason").fill("Site policy: Matching 95-second pallet obstructions at East exit are high severity. This is a standing rule.");
    await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
    await expect(form.getByRole("status")).toContainText("saved");

    // The worker generates the actual stored draft. Delay only its presentation
    // to reproducibly test a polling update arriving after the user starts typing.
    let reveal = false;
    await page.route("**/api/memory", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (!reveal) for (const item of body.guidance) {
        if (item.source_event_id === sourceId && item.status === "proposed") {
          item.draft_status = "running";
          item.draft_result = null;
          item.draft_history = [];
        }
      }
      await route.fulfill({ response, json: body });
    });
    await form.getByRole("button", { name: "Review suggested guidance" }).click();
    const card = page.locator(".guidance-card").first();
    await expect(card).toContainText("Drafting guidance");
    await card.getByRole("button", { name: "Review & approve" }).click();
    await expect(card.getByRole("combobox")).toHaveCount(0);
    await expect(card.locator(".guidance-effect")).toContainText("Safety event · high severity");
    const wording = `Supervisor wording ${viewport.width}: confirm matching 95-second pallet obstructions at East exit at high severity.`;
    await card.getByLabel("Reusable guidance").fill(wording);
    reveal = true;
    await expect(card).toContainText("Suggested wording", { timeout: 15000 });
    const explanation = card.locator(".draft-explanation");
    await expect(explanation).not.toHaveAttribute("open");
    await explanation.getByText("Suggested wording", { exact: true }).click();
    await expect(explanation).toContainText("Matches the standing site-policy example.");
    await explanation.getByText("Suggested wording", { exact: true }).click();
    await expect(card.getByLabel("Reusable guidance")).toHaveValue(wording);
    await expect(card).toContainText("Pending approval");
    await card.screenshot({ path: `${screenshots}/guidance-draft-${viewport.width}.png` });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

    await card.getByText("Draft history (1)", { exact: true }).click();
    await expect(card).toContainText("Mock call · no model tokens used.");
    await card.getByText("Exact draft input", { exact: true }).click();
    await expect(card.locator(".draft-attempt pre").first()).toContainText('"correction"');
    await expect(card.locator(".draft-attempt pre").first()).not.toContainText('"selection"');
    await card.getByText("Context & retrieval audit", { exact: true }).click();
    await expect(card.locator(".draft-attempt > details").filter({ has: page.getByText("Context & retrieval audit", { exact: true }) }).locator("pre")).toContainText('"selection"');
    await card.getByText("Exact draft output", { exact: true }).click();
    await expect(card.locator(".draft-attempt > details").filter({ has: page.getByText("Exact draft output", { exact: true }) }).locator("pre")).toContainText('"reusable": true');
    await card.getByText("Draft history (1)", { exact: true }).click();
    await page.route("**/api/guidance/*/approve", (route) => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Temporary test outage" }) }));
    await card.getByRole("button", { name: "Approve guidance" }).click();
    await expect(card.getByRole("alert")).toContainText("Temporary test outage");
    await expect(card.getByLabel("Reusable guidance")).toHaveValue(wording);
    await page.unroute("**/api/guidance/*/approve");
    await card.getByRole("button", { name: "Approve guidance" }).click();
    await expect(page.getByText("No suggested guidance yet.")).toBeVisible();
    await page.reload();
    const nav = viewport.width > 1000 ? page.locator(".sidebar") : page.getByRole("navigation", { name: "Mobile workspace" });
    await nav.getByRole("button", { name: viewport.width > 1000 ? "Memory & guidance" : "Guidance", exact: true }).click();
    await page.getByRole("button", { name: /^Approved/ }).click();
    await expect(page.locator(".guidance-card").first()).toContainText(wording);
    await expect(page.locator(".guidance-card").first().locator(".draft-explanation")).toHaveCount(0);
  });
}

test("a case-specific answer stays reviewable without inventing reusable wording", async ({ page }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to a disposable database");
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.getByTestId("event-00000003-0000-4000-8000-000000000002").click();
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: "Answer and resolve" }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  await form.getByLabel("Answer or reason").fill("yes");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(page.locator(".verdict")).toContainText("Alert dismissed");
  await form.getByRole("button", { name: "Review suggested guidance" }).click();
  const card = page.locator(".guidance-card").first();
  await expect(card).toContainText("No reusable wording proposed", { timeout: 15000 });
  await card.getByRole("button", { name: "Write reusable guidance" }).click();
  const wording = card.getByLabel("Reusable guidance");
  const approve = card.getByRole("button", { name: "Approve guidance", exact: true });
  await expect(wording).toHaveValue("");
  await expect(approve).toBeDisabled();
  await card.screenshot({ path: `${screenshots}/manual-guidance-blank.png` });
  for (const answer of ["yes", "ok", "   "]) {
    await wording.fill(answer);
    await expect(approve).toBeDisabled();
  }
  const id = (await card.getAttribute("data-testid")).slice("guidance-".length);
  const rejected = await page.request.post(`/api/guidance/${id}/approve`, { data: { text: "yes" } });
  expect(rejected.status()).toBe(422);
  await wording.fill("During the scheduled maintenance window, dismiss entries covered by a verified site-wide permit.");
  await expect(approve).toBeEnabled();
  await card.screenshot({ path: `${screenshots}/manual-guidance-required.png` });
  await card.getByRole("button", { name: "Cancel", exact: true }).click();
  await card.getByRole("button", { name: "Discard suggestion" }).click();
  await expect(page.getByText("No suggested guidance yet.")).toBeVisible();
  await page.getByRole("button", { name: "History", exact: true }).click();
  await page.locator(".guidance-card").first().getByRole("button", { name: "Open source event" }).click();
  await page.getByRole("button", { name: "Simulate similar event", exact: true }).click();
  await page.getByRole("button", { name: "Create event", exact: true }).click();
  await expect(page.getByLabel("Review and attempts")).toContainText("Was this person authorized", { timeout: 15000 });
  await expect(page.getByLabel("Review and attempts").locator(".verdict > strong")).toHaveText("Needs input");
});

test("a newer source correction prevents approval from a stale guidance editor", async ({ page, context }) => {
  test.skip(process.env.REVIEW_TEST_FEEDBACK !== "1", "Writes only to a disposable database");
  await page.goto("/");
  await signIn(page, "Asia supervisor");
  await page.getByTestId(`event-${sourceId}`).click();
  const form = page.getByLabel("Supervisor review", { exact: true });
  await form.getByRole("button", { name: /Correct this review|Update supervisor review/ }).click();
  await form.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("confirmed");
  await form.getByRole("combobox", { name: "Severity", exact: true }).selectOption("high");
  await form.getByLabel("Answer or reason").fill("Site policy: Matching 95-second exit obstructions are high severity.");
  await form.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(form.getByRole("status")).toContainText("saved");
  const before = await (await page.request.get("/api/memory")).json();
  const old = before.guidance.find(item => item.source_event_id === sourceId && item.status === "proposed");
  // Hold this tab's read snapshot to reproduce a supervisor acting before
  // polling learns that another tab replaced the source correction.
  await page.route("**/api/memory", route => route.fulfill({ json: before }));
  await form.getByRole("button", { name: "Review suggested guidance" }).click();
  const card = page.getByTestId(`guidance-${old.id}`);
  await card.getByRole("button", { name: "Review & approve" }).click();
  await card.getByLabel("Reusable guidance").fill("Keep this unsaved wording while checking the source.");

  const other = await context.newPage();
  await other.goto("/");
  await other.getByTestId(`event-${sourceId}`).click();
  const changed = other.getByLabel("Supervisor review", { exact: true });
  await changed.getByRole("button", { name: "Update supervisor review" }).click();
  await changed.getByRole("combobox", { name: "Supervisor decision", exact: true }).selectOption("rejected");
  await changed.getByLabel("Answer or reason").fill("The corrected site rule dismisses this 95-second obstruction.");
  await changed.getByRole("button", { name: "Save supervisor review", exact: true }).click();
  await expect(other.locator(".verdict")).toContainText("Alert dismissed");

  await card.getByRole("button", { name: "Approve guidance", exact: true }).click();
  await expect(card.getByRole("alert")).toContainText("no longer awaiting approval");
  await expect(card.getByLabel("Reusable guidance")).toHaveValue("Keep this unsaved wording while checking the source.");
  const after = await (await page.request.get("/api/memory")).json();
  const obsolete = after.guidance.find(item => item.id === old.id);
  expect(obsolete.status).toBe("superseded");
  expect(obsolete.approved_text).toBeNull();
  await card.screenshot({ path: `${screenshots}/stale-guidance-approval.png` });
  await page.unroute("**/api/memory");
  await page.getByRole("button", { name: "Refresh memory", exact: true }).click();
  const latest = page.locator(".guidance-card").first();
  await expect(latest.locator(".guidance-effect")).toContainText("Alert dismissed");
  await expect(card).toHaveCount(0);
  await other.close();
});
