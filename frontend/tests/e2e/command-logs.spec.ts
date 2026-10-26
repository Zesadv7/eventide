import { test, expect } from "@playwright/test";
test("recorded command streams page separately and survive refresh", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  await page.getByRole("textbox", { name: "任务输入" }).fill("命令日志场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(
    page.getByText("命令日志检查完成", { exact: false }).first(),
  ).toBeVisible();
  await page.locator(".tool-activity > summary").first().click();
  await page
    .locator(".tool-activity details > summary")
    .filter({ hasText: "bash" })
    .first()
    .click();
  await page.locator(".command-log-entry > summary").first().click();
  const panel = page.locator(".command-log-panel").first();
  await panel
    .getByRole("button", { name: "读取完整日志", exact: true })
    .click();
  await expect(panel.locator("pre")).toContainText("BEGIN");
  await panel.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(panel.locator("pre")).toContainText("PAGE-MIDDLE-MARKER");
  await panel.getByRole("tab", { name: "stderr", exact: true }).click();
  await expect(panel.locator("pre")).toHaveText("ERR-STREAM-MARKER");
  await page.reload();
  await page.locator(".tool-activity > summary").first().click();
  await page
    .locator(".tool-activity details > summary")
    .filter({ hasText: "bash" })
    .first()
    .click();
  await page.locator(".command-log-entry > summary").first().click();
  await panel.getByRole("tab", { name: "stderr", exact: true }).click();
  await expect(panel.locator("pre")).toHaveText("ERR-STREAM-MARKER");
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
