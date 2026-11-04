import { expect, test } from "@playwright/test";

const tenantSlug = process.env.E2E_TEST_TENANT_SLUG;
const email = process.env.E2E_TEST_EMAIL;
const password = process.env.E2E_TEST_PASSWORD;

test("owner can sign in and use the dashboard without exposing the session token", async ({
  page,
  context,
}) => {
  test.skip(
    !tenantSlug || !email || !password,
    "E2E_TEST_TENANT_SLUG, E2E_TEST_EMAIL and E2E_TEST_PASSWORD are required",
  );

  await page.goto("/");
  await page.getByLabel("Business ID").fill(tenantSlug!);
  await page.getByLabel("Email address").fill(email!);
  await page.getByLabel("Password").fill(password!);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByRole("heading", { name: "Good day" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Appointments" })).toBeVisible();
  await expect(page.getByRole("button", { name: "WhatsApp inbox" })).toBeVisible();

  const sessionCookie = (await context.cookies()).find(
    (cookie) => cookie.name === "smeops_session",
  );
  expect(sessionCookie).toBeDefined();
  expect(sessionCookie?.httpOnly).toBe(true);
  expect(sessionCookie?.secure).toBe(true);

  const localStorageKeys = await page.evaluate(() => Object.keys(window.localStorage));
  expect(localStorageKeys).not.toContain("access_token");
  expect(localStorageKeys).not.toContain("smeops_session");

  await page.getByRole("button", { name: "WhatsApp inbox" }).click();
  await expect(page.getByRole("heading", { name: "WhatsApp inbox" })).toBeVisible();

  await page.getByRole("button", { name: "Services" }).click();
  await expect(page.getByRole("heading", { name: "Services" })).toBeVisible();
});
