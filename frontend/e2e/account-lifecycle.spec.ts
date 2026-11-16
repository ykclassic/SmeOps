import { expect, test } from "@playwright/test";

test("deactivated owner is limited to the reactivation flow", async ({ page }) => {
  test.skip(!process.env.E2E_DASHBOARD_URL, "E2E_DASHBOARD_URL is required");
  let reactivated = false;
  let submitted: Record<string, unknown> | null = null;

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\//, "");
    if (path === "auth/me") {
      return reactivated
        ? route.fulfill({ status: 200, json: { id: "owner-1", tenant_id: "tenant-1", email: "owner@example.com", role: "owner" } })
        : route.fulfill({ status: 401, json: { detail: "Invalid or expired authentication credentials" } });
    }
    if (path === "account/lifecycle") {
      return route.fulfill({ status: 200, json: { status: reactivated ? "active" : "deactivated", tenant_id: "tenant-1" } });
    }
    if (path === "account/reactivate" && request.method() === "POST") {
      submitted = request.postDataJSON() as Record<string, unknown>;
      reactivated = true;
      return route.fulfill({ status: 200, json: { status: "active", changed: true, tenant_id: "tenant-1" } });
    }
    if (path === "services" || path.startsWith("appointments") || path === "business-hours" || path.startsWith("whatsapp/conversations")) {
      return route.fulfill({ status: 200, json: [] });
    }
    if (path === "whatsapp/connection") return route.fulfill({ status: 200, json: null });
    if (path === "telegram/owner-link") return route.fulfill({ status: 200, json: { linked: false, status: "disconnected" } });
    if (path === "telegram/booking-link") return route.fulfill({ status: 200, json: { connected: false, booking_link: null } });
    return route.fulfill({ status: 404, json: { detail: "Not found" } });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Your account is deactivated" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Appointments" })).toHaveCount(0);
  await page.getByLabel("Type REACTIVATE to confirm").fill("REACTIVATE");
  await page.getByLabel("Current account password").fill("Correct-Horse-Battery-42");
  await page.getByRole("button", { name: "Reactivate account" }).click();
  await expect(page.getByRole("heading", { name: "Good day" })).toBeVisible();
  expect(submitted).toEqual({
    confirmation: "REACTIVATE",
    password: "Correct-Horse-Battery-42",
    reason: "Owner initiated reactivation from the SmeOps dashboard",
  });
});
