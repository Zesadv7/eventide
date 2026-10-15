import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("real run history: archived evidence, exact JSONL and reload without replacing active chat", async ({
  page,
}) => {
  test.setTimeout(90000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let activeRun = "";
  try {
    const workspace = (
      await (await page.request.get("/api/workspaces")).json()
    )[0].workspace_id;
    const historical = await page.request.post("/api/sessions", {
      data: { workspace_id: workspace },
    });
    expect(historical.ok()).toBe(true);
    const session = (await historical.json()).session_id;
    const accepted = await page.request.post(`/api/sessions/${session}/runs`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: {
        prompt: `停驻场景 history-${crypto.randomUUID()}`,
        mode: "plan",
        execution_target: "snapshot",
      },
    });
    expect(accepted.status()).toBe(202);
    const oldRun = (await accepted.json()).run_id;
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/runs/${oldRun}`)).json()).status,
      )
      .toBe("interrupted");
    const continued = await page.request.post(
      `/api/sessions/${session}/continue`,
      { headers: { "Idempotency-Key": crypto.randomUUID() } },
    );
    expect(continued.status()).toBe(202);
    const nextRun = (await continued.json()).run_id;
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/runs/${nextRun}`)).json())
            .status,
      )
      .toBe("completed");
    expect(
      (
        await page.request.patch(`/api/sessions/${session}`, {
          data: { title: "浏览器历史归档", archived: true },
        })
      ).ok(),
    ).toBe(true);

    await page.goto("./#/chat");
    await page
      .getByRole("button", { name: "＋ 新建对话", exact: true })
      .click();
    const input = page.getByRole("textbox", { name: "任务输入" });
    await expect(input).toBeEnabled();
    await input.fill(`停止场景 history-active-${crypto.randomUUID()}`);
    const activeReceipt = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        /\/api\/sessions\/[^/]+\/runs$/.test(response.url()),
    );
    await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
    const active = await (await activeReceipt).json();
    activeRun = active.run_id;
    await expect(page.getByText("生成中 · 尚未保存")).toBeVisible();
    await page.getByRole("link", { name: "↶ 历史运行", exact: true }).click();
    await page.getByLabel("项目会话（含归档）").selectOption(session);
    await expect(page.getByLabel("项目会话（含归档）")).toContainText(
      "浏览器历史归档 · 已归档",
    );
    await expect(page.locator(".run-history-list button")).toHaveCount(2);
    await page
      .locator(".run-history-list button")
      .filter({ hasText: oldRun })
      .click();
    const detail = page.getByRole("region", { name: "历史运行详情" });
    await expect(detail).toContainText(oldRun);
    await expect(
      detail.getByRole("heading", { name: "已中断", exact: true }),
    ).toBeVisible();
    await expect(detail.locator(".tool-evidence")).toHaveCount(6);
    await detail.locator(".tool-evidence summary").first().click();
    await expect(detail.locator(".tool-evidence pre").first()).toContainText(
      "E2E 演示工作区",
    );
    const downloadEvent = page.waitForEvent("download");
    await detail.getByRole("link", { name: "下载事件 JSONL" }).click();
    const download = await downloadEvent;
    const records = (await readFile((await download.path())!, "utf8"))
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    expect(records.length).toBeGreaterThan(6);
    expect(records.every((record) => record.run_id === oldRun)).toBe(true);
    expect(records.some((record) => record.type === "tool.completed")).toBe(
      true,
    );
    await page.reload();
    await expect(page).toHaveURL(
      new RegExp(`session=${session}&run=${oldRun}`),
    );
    await expect(detail).toContainText(oldRun);
    expect(
      await page.evaluate(() =>
        JSON.parse(sessionStorage.getItem("eventide.session")!),
      ),
    ).toBe(active.session_id);
    expect(
      (await (await page.request.get(`/api/runs/${activeRun}`)).json()).status,
    ).toBe("running");
    await page.getByRole("link", { name: "◌ 对话", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "■ 停止", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(() =>
        JSON.parse(sessionStorage.getItem("eventide.session")!),
      ),
    ).toBe(active.session_id);
    await page.request.post(`/api/runs/${activeRun}/cancel`);
    activeRun = "";
    await page.goto(`./#/history?session=${session}&run=${oldRun}`);
    await expect(detail).toContainText(oldRun);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: "test-results/history-desktop.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 320, height: 844 });
    await expect(page.locator(".sidebar")).not.toBeInViewport();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({ path: "test-results/history-mobile.png" });
    await detail.scrollIntoViewIfNeeded();
    await page.screenshot({ path: "test-results/history-mobile-detail.png" });
    expect(errors).toEqual([]);
  } finally {
    if (activeRun) await page.request.post(`/api/runs/${activeRun}/cancel`);
  }
});
