import { test, expect } from "@playwright/test";

test("saved DAG starts explicitly, passes dependencies and survives refresh and stop", async ({
  page,
}) => {
  await page.goto("./#/chat");
  const previous = page.url();
  await page
    .getByRole("button", { name: "新建对话 New chat", exact: true })
    .click();
  await expect(page).not.toHaveURL(previous);
  await page.getByRole("textbox", { name: "任务输入" }).fill("源码证据场景");
  const admitted = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/runs"),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const root = (await (await admitted).json()).run_id;
  await expect(
    page.getByText("源码证据检查完成", { exact: true }),
  ).toBeVisible();
  const panel = page.locator(".graph-panel");
  await panel.locator(":scope > summary").click();
  await panel
    .getByRole("textbox", { name: "任务图名称", exact: true })
    .fill("调查依赖");
  await panel
    .getByRole("textbox", { name: "task_1 的任务", exact: true })
    .fill("读取 src/demo.py");
  await panel.getByRole("button", { name: "添加任务", exact: true }).click();
  await panel
    .getByRole("textbox", { name: "task_2 的任务", exact: true })
    .fill("复核前置任务的结论");
  await panel
    .getByRole("checkbox", { name: "task_2 依赖 task_1", exact: true })
    .check();
  const saved = page.waitForResponse(
    (r) => r.request().method() === "POST" && r.url().endsWith("/graphs"),
  );
  await panel.getByRole("button", { name: "保存方案", exact: true }).click();
  const definition = await (await saved).json();
  expect(definition.started).toBe(false);
  expect(
    definition.nodes.every((n: { claim: unknown }) => n.claim === null),
  ).toBe(true);
  await page.reload();
  await panel.locator(":scope > summary").click();
  await expect(panel.getByText("调查依赖", { exact: true })).toBeVisible();
  await expect(panel.getByText("方案待开始", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "开始调查", exact: true }).click();
  await expect(panel.getByText(/2\s*\/\s*2 个任务已完成/)).toBeVisible();
  const completed = await (
    await page.request.get(`/api/graphs/${definition.graph_id}`)
  ).json();
  expect(completed.state).toBe("completed");
  expect(completed.nodes[1].depends_on).toEqual(["task_1"]);
  for (const node of completed.nodes) {
    const child = await (
      await page.request.get(`/api/runs/${node.result.run_id}`)
    ).json();
    expect(child.parent_run_id).toBe(root);
  }
  await page.reload();
  await panel.locator(":scope > summary").click();
  await expect(panel.getByText(/2\s*\/\s*2 个任务已完成/)).toBeVisible();
  const history = panel
    .getByRole("link", { name: "查看此任务的运行记录", exact: true })
    .first();
  await expect(history).toHaveAttribute(
    "href",
    new RegExp(completed.nodes[0].result.child_session_id),
  );
  await panel
    .getByRole("textbox", { name: "任务图名称", exact: true })
    .fill("待停止");
  await panel
    .getByRole("textbox", { name: "task_1 的任务", exact: true })
    .fill("图等待场景");
  await panel.getByRole("button", { name: "保存方案", exact: true }).click();
  const stopping = panel.locator(".saved-graph").filter({
    has: page.getByRole("heading", { name: "待停止", exact: true }),
  });
  await stopping.getByRole("button", { name: "开始调查", exact: true }).click();
  await expect(
    stopping.getByRole("button", { name: "停止任务图", exact: true }),
  ).toBeVisible();
  await stopping
    .getByRole("button", { name: "停止任务图", exact: true })
    .click();
  await expect(stopping.locator(".badge").first()).toHaveText("已停止");
  await page.setViewportSize({ width: 320, height: 800 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(
    (await (await page.request.get(`/api/runs/${root}/graphs`)).json()).graphs,
  ).toHaveLength(2);
});
