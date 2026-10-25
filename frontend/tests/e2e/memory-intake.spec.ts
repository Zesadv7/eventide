import { test, expect } from "@playwright/test";

test("natural memory save is visible in chat and undo remains revoked after refresh", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  await page
    .getByRole("textbox", { name: "任务输入" })
    .fill("以后这个项目都用中文");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText(/已记住：这个项目都用中文/)).toBeVisible();
  await expect(page.getByText(/原话未指定范围，已默认当前项目/)).toBeVisible();
  await page.getByRole("button", { name: "撤销这条记忆", exact: true }).click();
  await expect(page.getByText(/已撤销：历史保存事实保留/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/已撤销：历史保存事实保留/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "撤销这条记忆", exact: true }),
  ).toHaveCount(0);
});
