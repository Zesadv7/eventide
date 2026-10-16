import { test, expect } from "@playwright/test";

test("real index publication survives lost acceptance and refresh with exact usage navigation", async ({
  page,
}) => {
  const keys: string[] = [],
    errors: string[] = [];
  let run = "";
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/projects/*/indexes", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (keys.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      run = (await response.json()).run_id;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.goto("./#/knowledge");
  await expect(page.getByRole("heading", { name: "知识索引" })).toBeVisible();
  await page.getByRole("button", { name: "建立索引", exact: true }).click();
  await expect(
    page.getByText("上次接收尚未确认，重试沿用原请求身份与配置。"),
  ).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: "重试原索引请求", exact: true })
    .click();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  await expect(page).toHaveURL(new RegExp(`index=${run}`));
  await expect(
    page.locator("article").getByRole("heading", { name: "构建完成" }),
  ).toBeVisible();
  const detail = await (await page.request.get(`/api/indexes/${run}`)).json();
  expect(detail.published.stats.files).toBeGreaterThan(0);
  expect(detail.published.stats.generation).toBe(detail.published.generation);
  const history = await (
    await page.request.get(`/api/projects/${detail.project_id}/indexes`)
  ).json();
  expect(
    history.filter((item: { run_id: string }) => item.run_id === run),
  ).toHaveLength(1);
  expect(
    (await (await page.request.get(`/api/runs/${run}/usage`)).json())
      .attempt_count,
  ).toBe(0);
  await page.getByRole("link", { name: "查看用量与预算" }).click();
  await expect(
    page.getByRole("button", { name: "指定运行", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(`运行：${run}`, { exact: true })).toBeVisible();
  await page.goBack();
  await page.reload();
  await expect(page.locator("article")).toContainText("发布代次");
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/indexes-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
