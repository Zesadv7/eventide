import { test, expect } from "@playwright/test";

test("actual read batch shows stable order and evidence after refresh", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  await page.getByRole("textbox", { name: "任务输入" }).fill("只读并发场景");
  const admitted = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/runs"),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const root = (await (await admitted).json()).run_id;
  await expect(
    page.getByText("并发读取检查完成", { exact: true }),
  ).toBeVisible();
  const data = await (await page.request.get(`/api/runs/${root}`)).json();
  const calls = Object.values(data.tools) as {
    name: string;
    status: string;
    batch: {
      position: number;
      size: number;
      parallel: boolean;
      batch_id: string;
    };
  }[];
  expect(calls.map((c) => c.name)).toEqual([
    "read_file",
    "glob",
    "search_text",
  ]);
  expect(
    calls.every(
      (c) => c.batch.parallel && c.status === "completed" && c.batch.size === 3,
    ),
  ).toBe(true);
  expect(calls.map((c) => c.batch.position)).toEqual([0, 1, 2]);
  expect(new Set(calls.map((c) => c.batch.batch_id)).size).toBe(1);
  for (const reload of [false, true]) {
    if (reload) await page.reload();
    const tools = page.locator(".tool-activity");
    await tools.locator(":scope > summary").click();
    const entries = tools.locator(".tool-entry");
    await expect(entries).toHaveCount(3);
    for (let i = 0; i < 3; i++) {
      await entries.nth(i).locator(":scope > summary").click();
      await expect(
        entries.nth(i).getByText(new RegExp(`第\\s*${i + 1}\\s*/\\s*3 项`)),
      ).toBeVisible();
      await expect(entries.nth(i).locator("small")).toHaveText("已完成");
    }
  }
  await page.setViewportSize({ width: 320, height: 800 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
