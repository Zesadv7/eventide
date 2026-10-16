import { test, expect } from "@playwright/test";

test("an ambiguous accepted response survives refresh and replays exactly one Run", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page.getByRole("button", { name: "＋ 新建对话", exact: true }).click();
  await expect(page).not.toHaveURL(previous);
  const session = decodeURIComponent(
    new URL(page.url()).hash.split("/").at(-1)!,
  );
  const keys: string[] = [];
  const identities: string[] = [];
  await page.route(`**/api/sessions/${session}/runs`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    identities.push((await response.json()).run_id);
    if (keys.length === 1) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          code: "conflict",
          detail: "接收结果未知，请保留原请求",
          message: "接收结果未知，请保留原请求",
        }),
      });
    } else await route.fulfill({ response });
  });
  await page
    .getByRole("textbox", { name: "任务输入" })
    .fill("接收身份刷新核对");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const retry = page.getByRole("button", {
    name: "使用同一身份重试",
    exact: true,
  });
  await expect(retry).toBeVisible();
  await page.reload();
  await expect(retry).toBeVisible();
  expect(keys).toHaveLength(1);
  await retry.click();
  await expect(retry).toHaveCount(0);
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(identities[1]).toBe(identities[0]);
  const runs = await (
    await page.request.get(`/api/sessions/${session}/runs`)
  ).json();
  expect(runs).toHaveLength(1);
  const messages = await (
    await page.request.get(`/api/sessions/${session}/messages`)
  ).json();
  expect(
    messages.filter((message: { role: string }) => message.role === "user"),
  ).toHaveLength(1);
});
