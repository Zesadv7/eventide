import { test, expect } from "@playwright/test";
test("questions survive browser reload, appear in attention and answer only the original call", async ({
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
  await page.getByRole("textbox", { name: "任务输入" }).fill("主动提问场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const panel = page.locator(".run-questions");
  await expect(
    panel.getByText("这个项目以后使用哪种语言？", { exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "提交回答", exact: true }),
  ).toBeDisabled();
  const runs = await (
    await page.request.get(`/api/sessions/${session}/runs`)
  ).json();
  const run = runs[0].id;
  const before = await (
    await page.request.get(`/api/runs/${run}/questions`)
  ).json();
  await page.reload();
  await expect(
    panel.getByText(before.questions[0].question_id, { exact: true }),
  ).toBeVisible();
  await page.goto("./#/attention");
  await expect(
    page.getByText("这个项目以后使用哪种语言？", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "打开提问对话", exact: true }).click();
  await expect(page).toHaveURL(chat);
  await panel.getByRole("button", { name: "中文", exact: true }).click();
  await panel
    .getByRole("textbox", { name: "问题1的自由输入" })
    .fill("保留英文函数名");
  await panel.getByRole("button", { name: "提交回答", exact: true }).click();
  await expect(
    page.getByText("已收到你的回答，继续原任务。", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    panel.getByText("中文；保留英文函数名", { exact: true }),
  ).toBeVisible();
  expect(
    await (await page.request.get(`/api/sessions/${session}/runs`)).json(),
  ).toHaveLength(1);
  const after = await (
    await page.request.get(`/api/runs/${run}/questions`)
  ).json();
  expect(after.questions[0].question_id).toBe(before.questions[0].question_id);
  expect(after.questions[0].status).toBe("answered");
  await page.setViewportSize({ width: 320, height: 750 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.goto("./#/attention");
  await expect(
    page.getByText("当前没有待回答的问题。", { exact: true }),
  ).toBeVisible();
});
