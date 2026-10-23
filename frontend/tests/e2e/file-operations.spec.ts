import { test as base, expect, type Page } from "@playwright/test";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import type { Run } from "../../src/api/types";
const execute = promisify(execFile);
const dirty = '# user draft preserved\nvalue = "original"\n';
const modified =
  '# user draft preserved\nvalue = "<script>alert(1)</script>"\n';
const test = base.extend<{ project: { root: string; session: string } }>({
  project: async ({ page }, use) => {
    const root = await mkdtemp(join(tmpdir(), "eventide-file-operations-e2e-"));
    try {
      await writeFile(join(root, "app.py"), 'value = "original"\n');
      await execute("git", ["-C", root, "init"]);
      await execute("git", ["-C", root, "add", "app.py"]);
      await execute("git", [
        "-C",
        root,
        "-c",
        "user.name=File Operations E2E",
        "-c",
        "user.email=files@example.com",
        "commit",
        "-m",
        "file fixture",
      ]);
      await writeFile(join(root, "app.py"), dirty);
      const registration = await page.request.post("/api/workspaces", {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { path: root },
      });
      expect(registration.status()).toBe(201);
      const workspace = await registration.json();
      const response = await page.request.post("/api/sessions", {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { workspace_id: workspace.workspace_id },
      });
      expect(response.status()).toBe(201);
      const session: string = (await response.json()).session_id;
      await page.goto(`./#/chat/${session}`);
      await expect(
        page.getByRole("textbox", { name: "任务输入" }),
      ).toBeEnabled();
      await page.getByLabel("运行模式", { exact: true }).selectOption("agent");
      await page.getByLabel("执行位置", { exact: true }).selectOption("source");
      await use({ root, session });
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  },
});
async function run(page: Page, prompt: string) {
  await page
    .getByRole("textbox", { name: "任务输入" })
    .fill(`${prompt} ${crypto.randomUUID()}`);
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/runs"),
  );
  await page.getByRole("button", { name: "发送 ↑", exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  const receipt = await response.json();
  await expect
    .poll(async () => {
      const result: Run = await (
        await page.request.get(`/api/runs/${receipt.run_id}`)
      ).json();
      if (["failed", "interrupted"].includes(result.status))
        throw new Error(JSON.stringify(result));
      return result.status;
    })
    .toBe("completed");
}
async function inspect(page: Page, path: string) {
  await page.locator(".tool-activity > summary").click();
  await page.locator(".file-operations > summary").click();
  await page
    .locator(".operation-list button")
    .filter({ hasText: path })
    .click();
  await expect(page.getByLabel("单次文件操作详情")).toBeVisible();
}
async function undo(page: Page, status = 200) {
  const reply = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith("/undo"),
  );
  await page.getByRole("button", { name: "撤销本次修改", exact: true }).click();
  const response = await reply;
  expect(response.status()).toBe(status);
  return response.json();
}

test("ordinary edit shows only its own diff and undo restores the prior dirty file after reload", async ({
  page,
  project,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await run(page, "文件操作编辑场景");
  expect(await readFile(join(project.root, "app.py"), "utf8")).toBe(modified);
  await inspect(page, "app.py");
  await expect(page.locator(".diff-removed")).toContainText(
    'value = "original"',
  );
  await expect(page.locator(".diff-added")).toContainText(
    "<script>alert(1)</script>",
  );
  await expect(page.locator(".diff-context")).toContainText(
    "# user draft preserved",
  );
  await expect(page.locator(".diff-viewer script")).toHaveCount(0);
  const currentRoute = page.url();
  await page.locator(".diff-files button").click();
  await expect(page).toHaveURL(currentRoute);
  await expect(page.locator(".diff-removed .line-number").first()).toHaveText(
    "2",
  );
  await page.reload();
  await inspect(page, "app.py");
  const operation = await undo(page);
  expect(operation.status).toBe("undone");
  expect(await readFile(join(project.root, "app.py"), "utf8")).toBe(dirty);
  const git = await execute("git", [
    "-C",
    project.root,
    "diff",
    "--",
    "app.py",
  ]);
  expect(git.stdout).toContain("+# user draft preserved");
  expect(git.stdout).not.toContain("<script>");
  await expect(
    page.getByText("本次修改已撤销。", { exact: true }),
  ).toBeVisible();
  const repeated = await page.request.post(
    `/api/sessions/${project.session}/file-operations/${operation.operation_id}/undo`,
    { headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
  expect(repeated.status()).toBe(200);
  expect((await repeated.json()).status).toBe("undone");
  expect(await readFile(join(project.root, "app.py"), "utf8")).toBe(dirty);
  await page.reload();
  await inspect(page, "app.py");
  await expect(page.getByLabel("单次文件操作详情")).toContainText("已撤销");
  await expect(
    page.getByRole("button", { name: "撤销本次修改", exact: true }),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("new builtin file has an added diff and undo removes only that file", async ({
  page,
  project,
}) => {
  await run(page, "文件操作新建场景");
  expect(await readFile(join(project.root, "new-created.txt"), "utf8")).toBe(
    "created <img src=x onerror=alert(1)>\n",
  );
  await inspect(page, "new-created.txt");
  await expect(page.getByLabel("单次文件操作详情")).toContainText("新增文件");
  await expect(page.locator(".diff-added")).toContainText("created <img");
  await expect(page.locator(".diff-viewer img")).toHaveCount(0);
  await expect(page.locator(".diff-removed")).toHaveCount(0);
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await undo(page);
  await expect
    .poll(async () => {
      try {
        await stat(join(project.root, "new-created.txt"));
        return true;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
        throw error;
      }
    })
    .toBe(false);
  expect((await stat(project.root)).isDirectory()).toBe(true);
  expect(await readFile(join(project.root, "app.py"), "utf8")).toBe(dirty);
});

test("later user modification causes a visible undo conflict and remains untouched", async ({
  page,
  project,
}) => {
  await run(page, "文件操作编辑场景");
  await inspect(page, "app.py");
  const later = `${modified}# later external edit\n`;
  await writeFile(join(project.root, "app.py"), later);
  await undo(page, 409);
  await expect(page.locator(".file-operations [role=alert]")).toContainText(
    "撤销冲突",
  );
  await expect(page.locator(".file-operations [role=alert]")).toContainText(
    "未覆盖当前文件",
  );
  expect(await readFile(join(project.root, "app.py"), "utf8")).toBe(later);
  await expect(page.getByText("本次修改已撤销。", { exact: true })).toHaveCount(
    0,
  );
  await page.reload();
  await inspect(page, "app.py");
  expect(await readFile(join(project.root, "app.py"), "utf8")).toBe(later);
});
