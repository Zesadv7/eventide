import { test, expect } from "@playwright/test";

test("real probe API: lost response, reload, exact retry and unchanged saved connection", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const before = await (await page.request.get("/api/config/provider")).json();
  const keys: string[] = [];
  let originalRun = "";
  await page.route("**/api/config/provider/test", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]!);
    if (keys.length === 1) {
      const result = await (await route.fetch()).json();
      originalRun = result.run_id;
      expect(result.success).toBe(true);
      expect(result.usage.attempt_count).toBe(1);
      await route.abort("failed");
    } else await route.continue();
  });
  const candidate = async () => {
    await page
      .getByLabel("默认模型", { exact: true })
      .fill("vue-probe-candidate");
    await page
      .getByLabel("API Key", { exact: true })
      .fill("browser-probe-fixture-key");
  };
  await page.goto("./#/settings");
  await expect(page.getByLabel("默认模型", { exact: true })).toHaveValue(
    before.model,
  );
  expect(keys).toHaveLength(0);
  await candidate();
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(page.getByLabel("默认模型", { exact: true })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存连接", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText(/上次连接测试结果尚未确认/)).toBeVisible();
  expect(keys).toHaveLength(1);
  const storage = await page.evaluate(() =>
    JSON.stringify({ ...sessionStorage, ...localStorage }),
  );
  expect(storage).not.toContain("browser-probe-fixture-key");
  expect(storage).not.toContain("vue-probe-candidate");
  await page.reload();
  await expect(
    page.getByRole("button", { name: "重试原连接测试", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
  await candidate();
  await page
    .getByRole("button", { name: "重试原连接测试", exact: true })
    .click();
  const result = page.getByRole("region", { name: "连接测试结果" });
  await expect(
    result.getByRole("heading", { name: "连接测试成功" }),
  ).toBeVisible();
  await expect(result).toContainText(originalRun);
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(
    (await (await page.request.get(`/api/runs/${originalRun}/usage`)).json())
      .attempt_count,
  ).toBe(1);
  expect(await (await page.request.get("/api/config/provider")).json()).toEqual(
    before,
  );
  await expect(
    page.getByText(`使用已保存的模型`, { exact: false }),
  ).toContainText(before.model);
  await expect(page.getByText(/UTC当日预算/)).toBeVisible();
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) =>
        key.startsWith("eventide.probe"),
      ),
    ),
  ).toEqual([]);
  await page.screenshot({
    path: "test-results/probe-desktop.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("real probe failure keeps unknown usage visible and creates no saved configuration", async ({
  page,
}) => {
  const before = await (await page.request.get("/api/config/provider")).json();
  await page.goto("./#/settings");
  await expect(page.getByLabel("默认模型", { exact: true })).toHaveValue(
    before.model,
  );
  await page.getByLabel("默认模型", { exact: true }).fill("vue-probe-failure");
  const response = page.waitForResponse((value) =>
    value.url().endsWith("/api/config/provider/test"),
  );
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  const evidence = await (await response).json();
  const result = page.getByRole("region", { name: "连接测试结果" });
  await expect(
    result.getByRole("heading", { name: "连接测试未成功" }),
  ).toBeVisible();
  await expect(result).toContainText(evidence.run_id);
  await expect(result).toContainText("未结算额度继续保留");
  await expect(result).toContainText("未报告");
  expect(evidence.usage.unknown_attempts).toBe(1);
  expect(evidence.usage.reported_totals).toEqual({});
  expect(await (await page.request.get("/api/config/provider")).json()).toEqual(
    before,
  );
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  await expect(result).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/probe-mobile.png",
    fullPage: true,
  });
});
