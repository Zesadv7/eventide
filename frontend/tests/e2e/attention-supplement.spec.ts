import { test, expect } from "@playwright/test";

test("supplement is saved during approval and attention action stays attached to the same run", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  const chat = page.url();
  const session = decodeURIComponent(new URL(chat).hash.split("/").at(-1)!);
  await page.getByRole("textbox", { name: "任务输入" }).fill("审批场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByRole("region", { name: "工具审批" })).toBeVisible();
  const panel = page.locator(".run-supplements");
  await panel.locator("summary").click();
  await panel
    .getByRole("textbox", { name: "运行中补充内容" })
    .fill("补充：请说明刚才的检查结果");
  await panel.getByRole("button", { name: "发送补充", exact: true }).click();
  await expect(
    panel.getByText("等待下一模型边界", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await page.locator(".run-supplements summary").click();
  await expect(
    page
      .locator(".run-supplements")
      .getByText("补充：请说明刚才的检查结果", { exact: true }),
  ).toBeVisible();
  await page.goto("./#/attention");
  await expect(page.getByRole("heading", { name: "待处理中心" })).toBeVisible();
  await expect(page.getByText("rm scratch.txt", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "拒绝", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "拒绝", exact: true }),
  ).toHaveCount(0);
  await page.goto(chat);
  await page.locator(".run-supplements summary").click();
  await expect(
    page.locator(".run-supplements").getByText("已消费一次", { exact: true }),
  ).toBeVisible();
  const runs = await (
    await page.request.get(`/api/sessions/${session}/runs`)
  ).json();
  expect(runs).toHaveLength(1);
  const received = await (
    await page.request.get(`/api/runs/${runs[0].id}/supplements`)
  ).json();
  expect(received.supplements).toHaveLength(1);
  expect(received.supplements[0].status).toBe("consumed");
});
