import { test, expect } from "@playwright/test";

test("offline progress reconnects at its latest cursor without submitting another Run", async ({
  page,
  context,
}) => {
  const streamUrls: string[] = [];
  let submissions = 0;
  page.on("request", (request) => {
    if (request.url().includes("/events?")) streamUrls.push(request.url());
    if (
      request.method() === "POST" &&
      /\/sessions\/[^/]+\/runs$/.test(request.url())
    )
      submissions += 1;
  });
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  const session = decodeURIComponent(
    new URL(page.url()).hash.split("/").at(-1)!,
  );
  await page
    .getByRole("textbox", { name: "任务输入" })
    .fill("并发快照场景：断网续接");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("生成中 · 尚未保存")).toBeVisible();
  await context.setOffline(true);
  await expect(
    page.getByText(/网络已离线，联网后会自动接回任务进度/),
  ).toBeVisible();
  await expect(page.getByText("生成中 · 尚未保存")).toHaveCount(0);
  await context.setOffline(false);
  await expect(
    page.getByText("快照已完成：并发快照场景：断网续接", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  expect(submissions).toBe(1);
  expect(streamUrls.length).toBeGreaterThanOrEqual(2);
  expect(
    Number(new URL(streamUrls.at(-1)!).searchParams.get("after")),
  ).toBeGreaterThan(0);
  const runs = await (
    await page.request.get(`/api/sessions/${session}/runs`)
  ).json();
  expect(runs).toHaveLength(1);
  await page.reload();
  await expect(
    page.getByText("快照已完成：并发快照场景：断网续接", { exact: true }),
  ).toBeVisible();
  expect(submissions).toBe(1);
});
