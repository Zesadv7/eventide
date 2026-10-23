import { test as base, expect, type Page } from "@playwright/test";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { Run, Workspace } from "../../src/api/types";

const execute = promisify(execFile);
const sourceCode =
  '"""demo retrieval source evidence."""\n\ndef main():\n    return "source"\n';
const test = base.extend<{
  project: { workspace: Workspace; root: string; session: string };
}>({
  project: async ({ page }, use) => {
    const root = await mkdtemp(join(tmpdir(), "eventide-retrieval-e2e-"));
    try {
      await mkdir(join(root, "src"));
      await writeFile(join(root, "src/demo.py"), sourceCode);
      await writeFile(
        join(root, "README.md"),
        "# Retrieval fixture\nRead src/demo.py for demo evidence.\n",
      );
      await execute("git", ["-C", root, "init"]);
      await execute("git", ["-C", root, "add", "-A"]);
      await execute("git", [
        "-C",
        root,
        "-c",
        "user.name=Retrieval E2E",
        "-c",
        "user.email=retrieval@example.com",
        "commit",
        "-m",
        "retrieval fixture",
      ]);
      const registration = await page.request.post("/api/workspaces", {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { path: root },
      });
      expect(registration.status()).toBe(201);
      const workspace: Workspace = await registration.json();
      const response = await page.request.post("/api/sessions", {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { workspace_id: workspace.workspace_id },
      });
      expect(response.ok()).toBe(true);
      const session: string = (await response.json()).session_id;
      await page.goto(`./#/chat/${session}`);
      await expect(
        page.getByRole("textbox", { name: "任务输入" }),
      ).toBeEnabled();
      await use({ workspace, root, session });
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  },
});

async function submit(page: Page, prompt: string): Promise<string> {
  await page.getByRole("textbox", { name: "任务输入" }).fill(prompt);
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/runs"),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  return (await response.json()).run_id;
}

async function completedRun(page: Page, id: string): Promise<Run> {
  await expect
    .poll(async () => {
      const value: Run = await (
        await page.request.get(`/api/runs/${id}`)
      ).json();
      if (["failed", "interrupted"].includes(value.status))
        throw new Error(JSON.stringify(value));
      return value.status;
    })
    .toBe("completed");
  return (await page.request.get(`/api/runs/${id}`)).json();
}

function retrievalTool(run: Run) {
  return Object.values(run.tools || {}).find(
    (value) => (value as { name?: string }).name === "search_knowledge",
  ) as {
    metadata: {
      retrieval: {
        workspace_id: string;
        index_generation: number;
        hits: {
          file: string;
          freshness: string;
          indexed_content_hash: string;
        }[];
      };
    };
  };
}

test("source preparation exposes real retrieval provenance and restores it after reload", async ({
  page,
  project,
}) => {
  await page.getByLabel("运行模式", { exact: true }).selectOption("plan");
  await page.getByLabel("任务开始前准备知识索引", { exact: true }).check();
  await page.getByLabel("按函数与类切分", { exact: true }).check();
  await page.getByLabel("索引范围", { exact: true }).selectOption("all");
  const id = await submit(page, "检索旅程 source");
  const run = await completedRun(page, id);
  expect(run.retrieval).toMatchObject({
    status: "available",
    workspace_id: project.workspace.workspace_id,
    location: "managed",
  });
  const evidence = retrievalTool(run).metadata.retrieval;
  expect(evidence.workspace_id).toBe(project.workspace.workspace_id);
  expect(evidence.index_generation).toBe(run.retrieval!.generation);
  expect(evidence.hits).toContainEqual(
    expect.objectContaining({
      file: "src/demo.py",
      freshness: "fresh",
      indexed_content_hash: createHash("sha256")
        .update(sourceCode)
        .digest("hex"),
    }),
  );
  await expect(
    page.getByRole("status", { name: "知识检索状态" }),
  ).toContainText("可用");
  await page.locator(".tool-activity > summary").click();
  await page
    .locator(".tool-entry > summary")
    .filter({ hasText: "search_knowledge" })
    .click();
  await expect(page.getByRole("region", { name: "检索来源" })).toContainText(
    "核对时一致",
  );
  await page.reload();
  await expect(
    page.getByRole("status", { name: "知识检索状态" }),
  ).toContainText(project.workspace.workspace_id);
  await expect(
    page.getByRole("status", { name: "知识检索状态" }),
  ).toContainText(`索引代次：${run.retrieval!.generation}`);
  await page.goto("./#/knowledge");
  await expect(page.getByLabel("索引目标", { exact: true })).toHaveValue(
    project.workspace.workspace_id,
  );
  await expect(
    page.getByRole("status", { name: "知识检索状态" }),
  ).toContainText("不代表全部源码最新");
  expect(await readFile(join(project.root, "src/demo.py"), "utf8")).toBe(
    sourceCode,
  );
  await expect(
    readFile(join(project.root, ".eventide/index.sqlite")),
  ).rejects.toMatchObject({ code: "ENOENT" });
});

test("ordinary worktree preparation uses its own modified source and index", async ({
  page,
  project,
}) => {
  await page.getByLabel("执行位置", { exact: true }).selectOption("worktree");
  await page.getByLabel("任务开始前准备知识索引", { exact: true }).check();
  await page.getByLabel("按函数与类切分", { exact: true }).check();
  await page.getByLabel("索引范围", { exact: true }).selectOption("all");
  const first = await completedRun(
    page,
    await submit(page, "检索旅程 worktree first"),
  );
  const actual = first.execution!.execution_workspace_id!;
  expect(actual).not.toBe(project.workspace.workspace_id);
  expect(first.retrieval).toMatchObject({
    status: "available",
    workspace_id: actual,
  });
  expect(retrievalTool(first).metadata.retrieval.workspace_id).toBe(actual);
  const copy: Workspace = await (
    await page.request.get(`/api/workspaces/${actual}`)
  ).json();
  const updated = sourceCode.replace('"source"', '"worktree-only"');
  await writeFile(join(copy.path, "src/demo.py"), updated);
  const second = await completedRun(
    page,
    await submit(page, "检索旅程 worktree updated"),
  );
  expect(second.retrieval!.generation).toBeGreaterThan(
    first.retrieval!.generation!,
  );
  expect(retrievalTool(second).metadata.retrieval.hits).toContainEqual(
    expect.objectContaining({
      file: "src/demo.py",
      freshness: "fresh",
      indexed_content_hash: createHash("sha256").update(updated).digest("hex"),
    }),
  );
  expect(await readFile(join(project.root, "src/demo.py"), "utf8")).toBe(
    sourceCode,
  );
  const original = await (
    await page.request.get(
      `/api/workspaces/${project.workspace.workspace_id}/index-status`,
    )
  ).json();
  expect(original.status).toBe("unavailable");
  await page.reload();
  await expect(page.getByLabel("执行位置", { exact: true })).toHaveValue(
    "worktree",
  );
  await expect(
    page.getByRole("status", { name: "知识检索状态" }),
  ).toContainText(actual);
  await page.goto("./#/knowledge");
  await page.getByLabel("索引目标", { exact: true }).selectOption(actual);
  await expect(
    page.getByRole("status", { name: "知识检索状态" }),
  ).toContainText(`索引代次：${second.retrieval!.generation}`);
});

test("delivery Writer prepares and queries its own index before independent acceptance", async ({
  page,
  project,
}) => {
  await page.goto("./#/deliveries");
  await page.getByLabel("副本名称", { exact: true }).fill("retrieval-writer");
  const receipt = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/deliveries$/.test(response.url()),
  );
  await page.getByRole("button", { name: "创建交付副本", exact: true }).click();
  const identity = (await (await receipt).json()).delivery_id;
  await expect(page.getByLabel("修复目标", { exact: true })).toBeVisible();
  await page
    .getByLabel("修复目标", { exact: true })
    .fill("检索交付场景：先核对 demo，再写入结果文件。");
  await page
    .getByLabel("验收命令", { exact: false })
    .fill('test "$(cat delivery-result.txt)" = "retrieval accepted"');
  await page.getByLabel("每轮修复前准备知识索引", { exact: true }).check();
  await page.getByLabel("按函数与类切分", { exact: true }).check();
  await page.getByLabel("索引范围", { exact: true }).selectOption("all");
  await page.getByRole("button", { name: "开始有界修复", exact: true }).click();
  await expect
    .poll(
      async () => {
        const value = await (
          await page.request.get(`/api/deliveries/${identity}`)
        ).json();
        if (value.repair_status === "stopped")
          throw new Error(JSON.stringify(value));
        return value.repair_status;
      },
      { timeout: 30000 },
    )
    .toBe("passed");
  const delivery = await (
    await page.request.get(`/api/deliveries/${identity}`)
  ).json();
  expect(delivery.applied).toBe(false);
  expect(delivery.rounds).toHaveLength(1);
  const writer = await completedRun(page, delivery.rounds[0].worker_run_id);
  expect(writer.retrieval!.workspace_id).not.toBe(
    project.workspace.workspace_id,
  );
  expect(writer.retrieval).toMatchObject({
    status: "available",
    location: "managed",
  });
  expect(retrievalTool(writer).metadata.retrieval.workspace_id).toBe(
    writer.retrieval!.workspace_id,
  );
  await page.reload();
  await expect(page.locator("article")).toContainText("每轮 Writer 开始前准备");
  await expect(page.locator("article")).toContainText("验收通过");
  await expect(
    readFile(join(project.root, "delivery-result.txt")),
  ).rejects.toMatchObject({ code: "ENOENT" });
});

test("cancelling a preparation waiting for its workspace lock consumes no model attempt", async ({
  page,
  project,
}) => {
  const workspace = project.workspace.workspace_id;
  const held = await page.request.post(
    `/api/__e2e/retrieval-hold/${workspace}`,
  );
  expect(held.ok()).toBe(true);
  try {
    await page.getByLabel("任务开始前准备知识索引", { exact: true }).check();
    await page.getByLabel("按函数与类切分", { exact: true }).check();
    await page.getByLabel("索引范围", { exact: true }).selectOption("all");
    const id = await submit(page, "检索旅程 cancel waiting");
    await expect(
      page.getByRole("status", { name: "知识检索状态" }),
    ).toContainText("准备中");
    await page.reload();
    await expect(
      page.getByRole("status", { name: "知识检索状态" }),
    ).toContainText("准备中");
    await page.getByRole("button", { name: "■ 停止", exact: true }).click();
    await expect(
      page.getByRole("heading", {
        name: "已停驻，等待你决定下一步",
        exact: true,
      }),
    ).toBeVisible();
    const run: Run = await (await page.request.get(`/api/runs/${id}`)).json();
    expect(run.status).toBe("interrupted");
    expect(run.retrieval).toMatchObject({
      status: "failed",
      reason: "cancelled",
      workspace_id: workspace,
    });
    expect(
      (await (await page.request.get(`/api/runs/${id}/usage`)).json())
        .attempt_count,
    ).toBe(0);
    expect(
      (
        await (
          await page.request.get(`/api/workspaces/${workspace}/index-status`)
        ).json()
      ).status,
    ).toBe("unavailable");
    await page.reload();
    await expect(
      page.getByRole("status", { name: "知识检索状态" }),
    ).toContainText("cancelled");
  } finally {
    const released = await page.request.post(
      `/api/__e2e/retrieval-release/${workspace}`,
    );
    expect(released.ok()).toBe(true);
  }
});
