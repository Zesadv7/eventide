import { test, expect } from "@playwright/test";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { execFileSync } from "node:child_process";
test("reviewed legacy import survives lost receipt and preserves inert history", async ({
  page,
}) => {
  const directory = await mkdtemp(join(tmpdir(), "eventide-import-browser-"));
  const source = join(directory, "legacy.db");
  execFileSync(
    process.env.EVENTIDE_PYTHON!,
    [
      "-c",
      "import sys; from pathlib import Path; from tests.test_legacy_migration import legacy_database; legacy_database(Path(sys.argv[1])).close()",
      source,
    ],
    { cwd: resolve("..") },
  );
  const original = await readFile(source);
  const keys: string[] = [];
  let operation = "";
  try {
    await page.route("**/api/projects/*/history-imports", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      keys.push(route.request().headers()["idempotency-key"]!);
      if (keys.length === 1) {
        const response = await route.fetch();
        expect(response.status()).toBe(202);
        operation = (await response.json()).run_id;
        await route.abort();
      } else await route.continue();
    });
    await page.goto("./#/imports");
    await page.getByLabel("旧数据库路径").fill(source);
    await page
      .getByRole("button", { name: "预检旧数据库", exact: true })
      .click();
    const detail = page.getByRole("article", { name: "历史导入详情" });
    await expect(detail.getByRole("heading")).toHaveText("预检 · 已完成");
    await expect(
      page.getByRole("button", { name: "确认导入历史", exact: true }),
    ).toBeDisabled();
    await page.getByLabel("确认将这份预检内容导入当前项目的只读历史").check();
    await page
      .getByRole("button", { name: "确认导入历史", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "重试原导入请求" }),
    ).toBeVisible();
    await page.reload();
    await page.getByRole("button", { name: "重试原导入请求" }).click();
    await expect(detail.getByRole("heading")).toHaveText("导入 · 已完成");
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    const result = await (
      await page.request.get("/api/history-imports/" + operation)
    ).json();
    const session = result.result.first_session_id;
    expect(
      (await (await page.request.get("/api/sessions/" + session)).json())
        .legacy_imported,
    ).toBe(true);
    const refused = await page.request.post(
      "/api/sessions/" + session + "/runs",
      {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { prompt: "execute legacy" },
      },
    );
    expect(refused.status()).toBe(422);
    expect(await refused.text()).toMatch(/ordinary|control|Control/);
    await page.getByRole("link", { name: "查看导入历史" }).click();
    await expect(
      page.getByRole("region", { name: "旧版只读消息" }),
    ).toContainText("hello");
    await expect(
      page.getByRole("region", { name: "历史运行详情" }),
    ).toContainText("done");
    await page.reload();
    await expect(
      page.getByRole("region", { name: "旧版只读消息" }),
    ).toContainText("hello");
    await page.setViewportSize({ width: 320, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(await readFile(source)).toEqual(original);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
