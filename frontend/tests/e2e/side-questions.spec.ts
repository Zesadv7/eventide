import { test, expect } from "@playwright/test";

test("side investigates while main runs and explicit return survives refresh", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  await page.getByRole("textbox", { name: "任务输入" }).fill("恢复中心场景");
  const accepted = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/runs"),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const main = (await (await accepted).json()).run_id;
  await expect(page.getByText(/等待手动停止。/).first()).toBeVisible();
  const panel = page.locator(".run-side-questions");
  await panel.locator("summary").click();
  await panel
    .getByRole("textbox", { name: "旁枝提问内容" })
    .fill("解释一下 src/demo.py");
  const created = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" && r.url().endsWith("/side-questions"),
  );
  await panel
    .getByRole("button", { name: "发起旁枝提问", exact: true })
    .click();
  const record = await (await created).json();
  await expect(
    panel.getByText("旁枝调查完成：已读取独立只读副本。", { exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get(`/api/runs/${main}`)).json()).status,
  ).toBe("running");
  const side = await (
    await page.request.get(`/api/runs/${record.side_run_id}`)
  ).json();
  expect(side.tools).toBeTruthy();
  expect(side.parent_run_id).toBe(main);
  const before = await (
    await page.request.get(`/api/runs/${main}/supplements`)
  ).json();
  expect(before.supplements).toHaveLength(0);
  await panel
    .getByRole("button", { name: "回传答案到主任务", exact: true })
    .click();
  await expect(panel.getByText("已回传", { exact: true })).toBeVisible();
  await page.reload();
  await panel.locator("summary").click();
  await expect(panel.getByText("已回传", { exact: true })).toBeVisible();
  const after = await (
    await page.request.get(`/api/runs/${main}/supplements`)
  ).json();
  expect(after.supplements).toHaveLength(1);
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const stopped = await page.request.post(`/api/runs/${main}/cancel`, {
    headers: { "Idempotency-Key": "side-e2e-stop" },
  });
  expect(stopped.ok()).toBe(true);
});
