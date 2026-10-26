import { test, expect } from "@playwright/test";
test("recovery center stops an exact run and continues it with a durable source-bound receipt", async ({
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
  const original = (await (await accepted).json()).run_id;
  await expect(page.getByText(/等待手动停止。/).first()).toBeVisible();
  await page.goto(`./#/runs/${original}/recovery`);
  await page.getByRole("button", { name: "停止当前运行", exact: true }).click();
  await expect(page.getByText("已由用户停止", { exact: false })).toBeVisible();
  const continued = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/continue"),
  );
  await page
    .getByRole("button", { name: "Continue · 检查并继续", exact: true })
    .click();
  const response = await continued;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON()).toEqual({
    expected_run_id: original,
  });
  const successor = (await response.json()).run_id;
  await page.getByRole("link", { name: "查看续做运行", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${successor}/recovery$`));
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/runs/${successor}`)).json())
          .status,
    )
    .toBe("completed");
  await page.reload();
  await expect(page.getByText("已正常完成", { exact: true })).toBeVisible();
  await expect(page.locator(".recovery-panel")).toContainText(original);
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
