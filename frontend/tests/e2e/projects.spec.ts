import { test, expect } from "@playwright/test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";

test("project registration and removal retry lost responses across reload", async ({
  page,
}) => {
  const root = await mkdtemp(join(tmpdir(), "eventide-web-project-"));
  try {
    await writeFile(join(root, "keep.txt"), "retain");
    await page.goto("./#/projects");
    const keys: Record<string, string[]> = { POST: [], DELETE: [] };
    const dropped = new Set<string>();
    await page.route("**/api/workspaces**", async (route) => {
      const method = route.request().method();
      if (!keys[method]) return route.continue();
      keys[method]!.push(route.request().headers()["idempotency-key"]!);
      if (!dropped.has(method)) {
        dropped.add(method);
        await route.fetch();
        await route.abort();
      } else await route.continue();
    });
    await page.getByLabel("项目目录", { exact: true }).fill(root);
    await page.getByRole("button", { name: "注册项目", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await page.reload();
    await page.getByLabel("项目目录", { exact: true }).fill(root);
    await page.getByRole("button", { name: "注册项目", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("已注册");
    expect(keys.POST).toHaveLength(2);
    expect(keys.POST![0]).toBe(keys.POST![1]);
    await page
      .getByRole("button", { name: "选择移除 " + basename(root), exact: true })
      .click();
    await page.getByLabel("确认只移除项目记录").check();
    await page.getByRole("button", { name: "移除空项目", exact: true }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await page.reload();
    await page.getByLabel("确认只移除项目记录").check();
    await page.getByRole("button", { name: "移除空项目", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("磁盘文件保留");
    expect(keys.DELETE).toHaveLength(2);
    expect(keys.DELETE![0]).toBe(keys.DELETE![1]);
    expect(await readFile(join(root, "keep.txt"), "utf8")).toBe("retain");
    await page.setViewportSize({ width: 320, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
