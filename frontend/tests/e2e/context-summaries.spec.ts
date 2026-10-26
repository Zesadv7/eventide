import { test, expect } from "@playwright/test";
test("saved summary opens original evidence and persists across refresh", async ({
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
    .fill("摘要对照场景：不要修改 API。" + "详细调查记录。".repeat(600));
  const accepted = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/runs"),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const runId = (await (await accepted).json()).run_id;
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/runs/${runId}`)).json()).status,
    )
    .toBe("completed");
  const run = await (await page.request.get(`/api/runs/${runId}`)).json();
  const saved = await page.request.post(`/api/__e2e/summary/${run.session_id}`);
  expect(saved.ok()).toBe(true);
  expect((await saved.json()).saved).toBe(1);
  const panel = page.locator(".context-summaries");
  await panel.locator("summary").first().click();
  await expect(panel.getByText("不修改接口", { exact: true })).toBeVisible();
  await expect(panel.getByText("尚未确认原因", { exact: true })).toBeVisible();
  await panel
    .getByRole("button", { name: "查看原始消息 0", exact: true })
    .click();
  await expect(panel.locator(".summary-evidence")).toContainText(
    "不要修改 API",
  );
  await page.reload();
  await panel.locator("summary").first().click();
  await expect(panel.getByText("不修改接口", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
