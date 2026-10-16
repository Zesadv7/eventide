import { test, expect } from "@playwright/test";
test("worktree creation retry, keep, cleanup and history use real Git", async ({
  page,
}) => {
  const name = "web-" + crypto.randomUUID().slice(0, 8);
  const keys: string[] = [];
  let run = "";
  await page.route("**/api/projects/*/worktrees", async (route) => {
    if (
      route.request().method() !== "POST" ||
      route.request().postDataJSON().action !== "create"
    )
      return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (keys.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      run = (await response.json()).run_id;
      await route.abort();
    } else await route.continue();
  });
  await page.goto("./#/worktrees");
  await page.getByLabel("副本名称", { exact: true }).fill(name);
  await page.getByRole("button", { name: "创建副本", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "重试原副本请求" }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "重试原副本请求" }).click();
  await expect(
    page.getByRole("article", { name: "副本操作详情" }).getByRole("heading"),
  ).toContainText("已完成");
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  const detail = await (
    await page.request.get(`/api/worktree-operations/${run}`)
  ).json();
  expect(detail.result.base_commit).toMatch(/^[a-f0-9]{40}$/);
  expect(
    (await (await page.request.get(`/api/runs/${run}/usage`)).json())
      .attempt_count,
  ).toBe(0);
  await page
    .getByRole("button", { name: `标记保留 ${name}`, exact: true })
    .click();
  await expect(page.locator(".copy").filter({ hasText: name })).toContainText(
    "已标记保留",
  );
  await page.reload();
  await expect(page.locator(".copy").filter({ hasText: name })).toContainText(
    "已标记保留",
  );
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByLabel(`确认清理 ${name}`, { exact: true }).check();
  await page
    .getByRole("button", { name: `清理副本 ${name}`, exact: true })
    .click();
  await expect(page.locator(".copy").filter({ hasText: name })).toHaveCount(0);
  await expect(
    page.getByRole("article", { name: "副本操作详情" }).getByRole("heading"),
  ).toContainText("已完成");
  await page.reload();
  await expect(page.locator(".operation-list")).toContainText(name);
});
