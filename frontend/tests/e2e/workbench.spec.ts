import { test, expect, type Page } from "@playwright/test";
import { access, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
async function newConversation(page: Page) {
  const previous = page.url();
  await page.getByRole("button", { name: "＋ 新建对话", exact: true }).click();
  await expect(page).not.toHaveURL(previous);
  await expect(page.getByRole("textbox", { name: "任务输入" })).toBeEnabled();
}
test("real Skills API: reviewed import, explicit activation, resources and durable loaded record", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("./#/skills");
  await page.getByRole("button", { name: "检查导入内容", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "导入已检查版本" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "导入已检查版本" }).click();
  await expect(page.getByRole("status")).toContainText("已安装固定版本");
  const pin = page.getByLabel("code-review 固定版本");
  await expect(pin).toHaveValue("");
  await pin.selectOption({ index: 1 });
  await page.getByLabel("选择 user:code-review").check();
  await page.getByRole("button", { name: "保存项目配置" }).click();
  await expect(page.getByRole("status")).toContainText("配置已保存");
  await page.getByRole("button", { name: "查看资源", exact: true }).click();
  await expect(page.locator("pre")).toContainText("name: code-review");
  await page.reload();
  await expect(page.getByLabel("选择 user:code-review")).toBeChecked();
  await newConversation(page);
  await page.getByRole("textbox", { name: "任务输入" }).fill("技能场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("已加载审查技能", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "◇ Skills", exact: true }).click();
  await expect(page.getByText(/已加载 user:code-review/)).toBeVisible();
  await page.reload();
  await expect(page.getByText(/已加载 user:code-review/)).toBeVisible();
  await page.screenshot({
    path: "test-results/skills-desktop.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
test("real Runtime API: chat, preview, drafts, approval, usage, stop and Continue", async ({
  page,
  context,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("./#/chat");
  await newConversation(page);
  const input = page.getByRole("textbox", { name: "任务输入" });
  await input.fill("流式场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("生成中 · 尚未保存")).toBeVisible();
  await expect(
    page.getByText("流式场景已完成，最终内容以这条回复为准。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("生成中 · 尚未保存")).toHaveCount(0);
  await input.fill("第一个会话的草稿");
  const first = page.url();
  await newConversation(page);
  await expect(page).not.toHaveURL(first);
  await input.fill("第二个会话的草稿");
  const second = page.url();
  await page.goto(first);
  await expect(input).toHaveValue("第一个会话的草稿");
  await page.goto(second);
  await expect(input).toHaveValue("第二个会话的草稿");
  await input.fill("审批场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByRole("region", { name: "工具审批" })).toBeVisible();
  const monitor = await context.newPage();
  await monitor.goto(second);
  await expect(monitor.getByRole("region", { name: "工具审批" })).toBeVisible();
  await page.getByRole("link", { name: "▥ 用量", exact: true }).click();
  await expect(page.getByRole("heading", { name: "用量与成本" })).toBeVisible();
  await page.goto(second);
  await page.getByRole("button", { name: "批准这一次", exact: true }).click();
  await expect(page.getByText("审批场景已完成", { exact: true })).toBeVisible();
  await expect(
    monitor.getByText("审批场景已完成", { exact: true }),
  ).toBeVisible();
  await monitor.close();
  await page.reload();
  await expect(page.getByText("审批场景已完成", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "▥ 用量", exact: true }).click();
  await expect(page.getByRole("heading", { name: "请求明细" })).toBeVisible();
  await expect(page.locator(".stat-grid").getByText("供应商报告")).toHaveCount(
    0,
  );
  await expect(
    page.locator("tbody").getByText("供应商报告").first(),
  ).toBeVisible();
  await page.goto(second);
  await input.fill("停止场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await page.getByRole("button", { name: "■ 停止", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "放弃恢复", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toHaveCount(0);
  await newConversation(page);
  await expect(page).not.toHaveURL(second);
  await input.fill("停驻场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "检查并继续", exact: true }).click();
  await expect(page.getByText("已恢复完成", { exact: true })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/workbench-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  await expect(page.getByRole("button", { name: "切换导航" })).toBeVisible();
  await page.screenshot({
    path: "test-results/workbench-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("real budgets API: price versions, zero admission limit, reload and shared Continue", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("./#/usage");
  await page
    .getByRole("button", { name: "当前项目全部对话", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "共享预算", exact: true }),
  ).toBeVisible();
  await page.getByText(/管理价格版本（/).click();
  await page
    .getByLabel("价格对应的模型", { exact: true })
    .fill("vue-fixture-model");
  await page.getByLabel("每百万 Token 单价", { exact: true }).fill("1");
  await page.getByRole("button", { name: "保存价格版本", exact: true }).click();
  await expect(
    page.getByText(
      "价格版本已保存。历史费用快照保留原版本，新价格用于后续请求。",
    ),
  ).toBeVisible();
  await page
    .getByLabel("计费类别", { exact: true })
    .selectOption("output_tokens");
  await page.getByLabel("每百万 Token 单价", { exact: true }).fill("2");
  await page.getByRole("button", { name: "保存价格版本", exact: true }).click();
  await expect(page.getByText(/管理价格版本（2 条）/)).toBeVisible();
  await page.getByLabel("预算开关", { exact: true }).selectOption("true");
  await page.getByLabel("Token 上限", { exact: true }).fill("0");
  await page.getByRole("button", { name: "保存预算", exact: true }).click();
  await expect(
    page.getByText("已达到预算阈值，禁止新模型请求。"),
  ).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: "当前项目全部对话", exact: true })
    .click();
  await expect(page.getByLabel("Token 上限", { exact: true })).toHaveValue("0");
  await newConversation(page);
  await page.getByRole("textbox", { name: "任务输入" }).fill("预算阻止场景");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("本轮已停驻", { exact: true })).toBeVisible();
  await expect(page.locator(".parked-card")).toContainText("token_budget");
  const conversation = page.url();
  await page.getByRole("link", { name: "▥ 用量", exact: true }).click();
  await page.getByRole("button", { name: "最近运行", exact: true }).click();
  await expect(
    page
      .locator(".stat-grid article")
      .filter({ hasText: "调用尝试" })
      .locator("strong"),
  ).toHaveText("0");
  await page
    .getByRole("button", { name: "当前项目全部对话", exact: true })
    .click();
  await page.getByLabel("预算开关", { exact: true }).selectOption("false");
  await page.getByRole("button", { name: "保存预算", exact: true }).click();
  await expect(page.getByText("已关闭这一范围的预算限制。")).toBeVisible();
  await page.goto(conversation);
  await page.getByRole("button", { name: "检查并继续", exact: true }).click();
  await expect(
    page.getByText("已读取项目：预算阻止场景", { exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "▥ 用量", exact: true }).click();
  await page.getByRole("button", { name: "最近运行", exact: true }).click();
  await expect(page.getByText("CNY 已知费用", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "费用估算" })).toContainText(
    "当前范围估算完整",
  );
  await page.screenshot({
    path: "test-results/budget-costs.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/budget-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("real snapshot API: concurrent project conversations and independent stop", async ({
  page,
  context,
}) => {
  await page.goto("./#/chat");
  await newConversation(page);
  await page.getByLabel("运行模式", { exact: true }).selectOption("plan");
  await page.getByLabel("执行位置", { exact: true }).selectOption("snapshot");
  await page.getByRole("textbox", { name: "任务输入" }).fill("并发快照场景 A");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(
    page.getByText("本轮使用固定只读快照 · 源码变化不影响在途读取"),
  ).toBeVisible();
  const first = page.url();
  await newConversation(page);
  await page.getByLabel("运行模式", { exact: true }).selectOption("plan");
  await page.getByLabel("执行位置", { exact: true }).selectOption("snapshot");
  await page.getByRole("textbox", { name: "任务输入" }).fill("并发快照场景 B");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(
    page.getByText("本轮使用固定只读快照 · 源码变化不影响在途读取"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "■ 停止", exact: true }),
  ).toBeVisible();
  const monitor = await context.newPage();
  await monitor.goto(first);
  await expect(
    monitor.getByRole("button", { name: "■ 停止", exact: true }),
  ).toBeVisible();
  await monitor.getByRole("button", { name: "■ 停止", exact: true }).click();
  await expect(monitor.getByText("本轮已停驻", { exact: true })).toBeVisible();
  await expect(
    page.getByText("快照已完成：并发快照场景 B", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await page.reload();
  await expect(
    page.getByText("本轮使用固定只读快照 · 源码变化不影响在途读取"),
  ).toBeVisible();
  await monitor.close();
});

test("real worktree API: concurrent edits, source preservation and stable session copy", async ({
  page,
  context,
}) => {
  await page.goto("./#/chat");
  await expect(page.locator(".workspace-path")).toContainText("main");
  const source = join(
    dirname((await page.locator(".workspace-path").innerText()).trim()),
    "plain",
  );
  const original = await readFile(`${source}/README.md`, "utf8");
  await page.getByRole("button", { name: "添加项目", exact: true }).click();
  await page.getByLabel("本机项目绝对路径", { exact: true }).fill(source);
  await page
    .locator("form.add-workspace")
    .getByRole("button", { name: "添加项目", exact: true })
    .click();
  await expect(page.locator(".workspace-path")).toHaveText(source);
  await newConversation(page);
  await page.getByLabel("执行位置", { exact: true }).selectOption("worktree");
  await page.getByRole("textbox", { name: "任务输入" }).fill("副本写入场景 A");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("生成中 · 尚未保存")).toBeVisible();
  const first = page.url();
  await newConversation(page);
  await page.getByLabel("执行位置", { exact: true }).selectOption("worktree");
  await page.getByRole("textbox", { name: "任务输入" }).fill("副本写入场景 B");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(page.getByText("生成中 · 尚未保存")).toBeVisible();
  const monitor = await context.newPage();
  await monitor.goto(first);
  await expect(
    monitor.getByRole("button", { name: "■ 停止", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("副本已完成：副本写入场景 B", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await expect(
    monitor.getByText("副本已完成：副本写入场景 A", { exact: true }),
  ).toBeVisible();
  const readSession = async (url: string) => {
    const id = decodeURIComponent(new URL(url).hash.split("/").at(-1)!);
    return (
      await page.request.get(
        new URL(`/api/sessions/${encodeURIComponent(id)}`, page.url()).href,
      )
    ).json();
  };
  const one = await readSession(first),
    two = await readSession(page.url());
  expect(one.execution.execution_workspace_id).not.toBe(
    two.execution.execution_workspace_id,
  );
  const copies = await (
    await page.request.get(new URL("/api/workspaces", page.url()).href)
  ).json();
  for (const [record, text] of [
    [one, "副本写入场景 A"],
    [two, "副本写入场景 B"],
  ] as const) {
    const copy = copies.find(
      (item: { workspace_id: string }) =>
        item.workspace_id === record.execution.execution_workspace_id,
    );
    expect(await readFile(`${copy.path}/result.txt`, "utf8")).toBe(text);
  }
  expect(await readFile(`${source}/README.md`, "utf8")).toBe(original);
  await expect(access(join(source, "result.txt"))).rejects.toMatchObject({
    code: "ENOENT",
  });
  await page.reload();
  await expect(page.getByLabel("执行位置", { exact: true })).toHaveValue(
    "worktree",
  );
  await expect(page.getByLabel("执行位置", { exact: true })).toBeDisabled();
  await page
    .getByRole("textbox", { name: "任务输入" })
    .fill("副本写入场景 B 续写");
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  await expect(
    page.getByText("副本已完成：副本写入场景 B 续写", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  expect((await readSession(page.url())).execution.execution_workspace_id).toBe(
    two.execution.execution_workspace_id,
  );
  await monitor.close();
});
