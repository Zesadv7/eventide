import { test, expect } from "@playwright/test";

test("workbench boots after a compatible real contract check", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("./#/chat");
  await expect(page.getByRole("button", { name: /新建对话/ })).toBeVisible();
  const response = await page.request.get("/api/runtime/contract");
  expect(response.ok()).toBeTruthy();
  const contract = await response.json();
  expect(contract.contract_version).toBe(1);
  expect(contract.schema_version).toBeGreaterThanOrEqual(6);
  expect(Array.isArray(contract.capabilities)).toBe(true);
  expect(errors).toEqual([]);
});

test("a version mismatch shows the barrier, blocks writes and offers retry", async ({
  page,
}) => {
  await page.route("**/api/runtime/contract", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        contract_version: 99,
        schema_version: 6,
        runtime_version: "future",
        capabilities: [],
      }),
    }),
  );
  await page.goto("./#/chat");
  const barrier = page.getByRole("alertdialog");
  await expect(barrier).toBeVisible();
  await expect(barrier).toContainText("前后端版本不兼容");
  await expect(barrier).toContainText("v99");
  await expect(page.getByRole("button", { name: /新建对话/ })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "任务输入" })).toHaveCount(0);
  await expect(barrier.getByRole("button", { name: "重新检查" })).toBeVisible();
  await barrier.getByRole("button", { name: "重新检查" }).click();
  await expect(barrier).toBeVisible();
  await expect(page.getByRole("button", { name: /新建对话/ })).toHaveCount(0);
});

test("a transport failure is shown as unavailable, not as a version problem", async ({
  page,
}) => {
  await page.route("**/api/runtime/contract", (route) =>
    route.abort("connectionrefused"),
  );
  await page.goto("./#/chat");
  const barrier = page.getByRole("alertdialog");
  await expect(barrier).toBeVisible();
  await expect(barrier).toContainText("不是版本不匹配");
  await expect(barrier).not.toContainText("升级");
  await expect(barrier.getByRole("button", { name: "重新检查" })).toBeVisible();
});
