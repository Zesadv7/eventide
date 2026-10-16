import { test, expect } from "@playwright/test";

test("attachment lost upload reuses its identity after refresh and never creates a second file", async ({
  page,
}) => {
  await page.goto("./#/chat");
  await page.getByRole("button", { name: "＋ 新建对话", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "任务输入" })).toBeEnabled();
  const keys: string[] = [];
  let path = "";
  await page.route("**/api/sessions/*/attachments", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    path = new URL(route.request().url()).pathname;
    if (keys.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      await route.abort();
    } else await route.continue();
  });
  const file = {
    name: "retry-note.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("private local attachment"),
  };
  await page.locator('input[type="file"]').setInputFiles(file);
  await expect.poll(() => keys.length).toBe(1);
  await expect(page.locator('input[type="file"]')).toBeEnabled();
  await page.reload();
  await expect(page.locator('input[type="file"]')).toBeEnabled();
  await page.locator('input[type="file"]').setInputFiles(file);
  await expect.poll(() => keys.length).toBe(2);
  expect(keys[0]).toBe(keys[1]);
  await expect
    .poll(async () => (await (await page.request.get(path)).json()).length)
    .toBe(1);
  await expect(page.locator(".attachment-chips")).toContainText(
    "retry-note.txt",
  );
  await page.locator(".attachment-chips button").click();
  await expect
    .poll(async () => (await (await page.request.get(path)).json()).length)
    .toBe(0);
});
