import { test, expect } from "@playwright/test";

test("model retry survives reload and finishes within the original Run", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  const session = decodeURIComponent(
    new URL(page.url()).hash.split("/").at(-1)!,
  );
  await page.getByRole("textbox", { name: "任务输入" }).fill("模型重试场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText(/模型连接中断/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/模型连接中断/)).toBeVisible();
  await expect(page.getByText("模型自动恢复完成", { exact: true })).toBeVisible(
    { timeout: 15000 },
  );
  await expect(page.getByText(/模型连接中断/)).toHaveCount(0);
  const runs = await (
    await page.request.get(`/api/sessions/${session}/runs`)
  ).json();
  expect(runs).toHaveLength(1);
  const response = await page.request.get(`/api/runs/${runs[0].id}/diagnostics`);
  expect(response.ok()).toBe(true);
  const report = await response.json();
  expect(report.counts.attempts).toBe(2);
  expect(report.status).toBe("completed");
});
