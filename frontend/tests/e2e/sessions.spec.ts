import { test, expect } from "@playwright/test";
test("session management preserves retry identity and rejects stale tabs", async ({
  page,
  context,
}) => {
  const workspace = (
    await (await page.request.get("/api/workspaces")).json()
  )[0].workspace_id;
  const session = (
    await (
      await page.request.post("/api/sessions", {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { workspace_id: workspace },
      })
    ).json()
  ).session_id;
  await page.goto(`./#/sessions?session=${session}`);
  await expect(page.getByLabel("会话名称")).toBeVisible();
  const other = await context.newPage();
  await other.goto(`./#/sessions?session=${session}`);
  await expect(other.getByLabel("会话名称")).toBeVisible();
  const keys: string[] = [];
  let dropped = false;
  await page.route(`**/api/sessions/${session}`, async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (!dropped) {
      dropped = true;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page.getByLabel("会话名称").fill("已保存但响应丢失");
  await page.getByRole("button", { name: "保存名称", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "重试原会话管理请求" }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "重试原会话管理请求" }).click();
  await expect(page.getByRole("status")).toContainText("已保存");
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  await other.getByLabel("会话名称").fill("过时覆盖");
  await other.getByRole("button", { name: "保存名称", exact: true }).click();
  await expect(other.getByRole("alert")).toBeVisible();
  expect(
    (await (await page.request.get(`/api/sessions/${session}`)).json()).title,
  ).toBe("已保存但响应丢失");
  const refresh = other.getByRole("button", {
    name: "刷新会话版本",
    exact: true,
  });
  // Refreshing a stale tab must not silently discard its unsaved title.
  const cancelledDialog = other.waitForEvent("dialog");
  const cancelledRefresh = refresh.click();
  const cancel = await cancelledDialog;
  expect(cancel.type()).toBe("confirm");
  expect(cancel.message()).toBe("有尚未保存的修改，确定放弃并离开吗？");
  await cancel.dismiss();
  await cancelledRefresh;
  await expect(other.getByLabel("会话名称")).toHaveValue("过时覆盖");
  expect(
    (await (await page.request.get(`/api/sessions/${session}`)).json()).title,
  ).toBe("已保存但响应丢失");

  const acceptedDialog = other.waitForEvent("dialog");
  const acceptedRefresh = refresh.click();
  const accept = await acceptedDialog;
  expect(accept.type()).toBe("confirm");
  expect(accept.message()).toBe("有尚未保存的修改，确定放弃并离开吗？");
  await accept.accept();
  await acceptedRefresh;
  await expect(other.getByLabel("会话名称")).toHaveValue("已保存但响应丢失");
  await other.close();
  await page.getByRole("button", { name: "归档会话", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "恢复会话", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "恢复会话", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "归档会话", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByLabel("确认删除这条空会话").check();
  await page.getByRole("button", { name: "删除空会话", exact: true }).click();
  await expect
    .poll(async () =>
      (await page.request.get(`/api/sessions/${session}`)).status(),
    )
    .toBe(404);
});

test("new Session retries a lost acceptance across reload without duplicates", async ({
  page,
}) => {
  await page.goto("./#/");
  await expect(
    page.getByRole("button", { name: "新建对话 New chat", exact: true }),
  ).toBeEnabled();
  const workspace = (
    await (await page.request.get("/api/workspaces")).json()
  )[0].workspace_id;
  const path = "/api/workspaces/" + workspace + "/sessions";
  const before = (await (await page.request.get(path)).json()).length;
  const keys: string[] = [];
  let dropped = false;
  await page.route("**/api/sessions", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (!dropped) {
      dropped = true;
      await route.fetch();
      await route.abort();
    } else await route.continue();
  });
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect.poll(() => keys.length).toBe(2);
  expect(keys[0]).toBe(keys[1]);
  await expect
    .poll(async () => (await (await page.request.get(path)).json()).length)
    .toBe(before + 1);
});
