import { test, expect } from "@playwright/test";
test("real Runtime API: chat, preview, drafts, approval, usage, stop and Continue", async ({
  page,
  context,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/#/chat");
  await page.getByRole("button", { name: "＋ 新建对话", exact: true }).click();
  const input = page.getByRole("textbox", { name: "任务输入" });
  await input.fill("流式场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("生成中 · 尚未保存")).toBeVisible();
  await expect(
    page.getByText("流式场景已完成，最终内容以这条回复为准。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("生成中 · 尚未保存")).toHaveCount(0);
  await input.fill("第一个会话的草稿");
  const first = page.url();
  await page.getByRole("button", { name: "＋ 新建对话", exact: true }).click();
  await expect(page).not.toHaveURL(first);
  await input.fill("第二个会话的草稿");
  const second = page.url();
  await page.goto(first);
  await expect(input).toHaveValue("第一个会话的草稿");
  await page.goto(second);
  await expect(input).toHaveValue("第二个会话的草稿");
  await input.fill("审批场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByRole("region", { name: "工具审批" })).toBeVisible();
  const monitor = await context.newPage();
  await monitor.goto(second);
  await expect(monitor.getByRole("region", { name: "工具审批" })).toBeVisible();
  await page.getByRole("link", { name: "▥ 用量", exact: true }).click();
  await expect(page.getByRole("heading", { name: "用量与成本" })).toBeVisible();
  await page.goto(second);
  await page.getByRole("button", { name: "批准这一次", exact: true }).click();
  await expect(page.getByText("审批场景已完成", { exact: true })).toBeVisible();
  await expect(
    monitor.getByText("审批场景已完成", { exact: true }),
  ).toBeVisible();
  await monitor.close();
  await page.reload();
  await expect(page.getByText("审批场景已完成", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "▥ 用量", exact: true }).click();
  await expect(page.getByRole("heading", { name: "请求明细" })).toBeVisible();
  await expect(page.locator(".stat-grid").getByText("供应商报告")).toHaveCount(
    0,
  );
  await expect(
    page.locator("tbody").getByText("供应商报告").first(),
  ).toBeVisible();
  await page.goto(second);
  await input.fill("停止场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await page.getByRole("button", { name: "■ 停止", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "放弃恢复", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "＋ 新建对话", exact: true }).click();
  await expect(page).not.toHaveURL(second);
  await input.fill("停驻场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "检查并继续", exact: true }).click();
  await expect(page.getByText("已恢复完成", { exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/workbench-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  await expect(page.getByRole("button", { name: "切换导航" })).toBeVisible();
  await page.screenshot({
    path: "test-results/workbench-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
