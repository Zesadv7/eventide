import { test, expect } from "@playwright/test";

test("real bundled evaluation: lost acceptance, reload, exact retry and offline report", async ({
  page,
}) => {
  test.setTimeout(120000);
  const errors: string[] = [],
    keys: string[] = [];
  let original = "";
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/projects/*/evaluations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (keys.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      original = (await response.json()).evaluation_id;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.goto("./#/evaluations");
  await expect(
    page.getByRole("heading", { name: "离线机制自检" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "运行一次离线自检", exact: true }),
  ).toBeEnabled();
  expect(keys).toHaveLength(0);
  await page.getByLabel("任务划分").selectOption("holdout");
  await page
    .getByRole("button", { name: "运行一次离线自检", exact: true })
    .click();
  await expect(page.getByText(/上次评测接收尚未确认/)).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("任务划分")).toHaveValue("holdout");
  await expect(page.getByLabel("任务划分")).toBeDisabled();
  await page
    .getByRole("button", { name: "重试原评测请求", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`evaluation=${original}`));
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  const region = page.getByRole("region", { name: "评测运行详情" });
  await expect(
    region.getByRole("heading", { name: "执行完毕", exact: true }),
  ).toBeVisible({ timeout: 90000 });
  await expect(region.locator("tbody tr")).toHaveCount(8);
  await expect(region).toContainText("8 / 8");
  const detail = await (
    await page.request.get(`/api/evaluations/${original}`)
  ).json();
  expect(detail.passed).toBe(8);
  expect(detail.measurement_kind).toBe("scripted_mechanism_validation");
  expect(detail.billing_applicable).toBe(false);
  expect(
    (
      await (
        await page.request.get(`/api/projects/${detail.project_id}/evaluations`)
      ).json()
    ).filter(
      (value: { evaluation_id: string }) => value.evaluation_id === original,
    ),
  ).toHaveLength(1);
  expect(
    (await (await page.request.get(`/api/runs/${original}/usage`)).json())
      .attempt_count,
  ).toBe(0);
  await page.getByRole("button", { name: "读取完整报告", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "下载报告 JSON", exact: true }),
  ).toBeVisible();
  const report = await (
    await page.request.get(`/api/evaluations/${original}/report`)
  ).json();
  expect(report.schema_version).toBe(2);
  expect(report.results).toHaveLength(8);
  expect(report.billing_applicable).toBe(false);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/evaluations-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  await expect(region).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/evaluations-mobile.png",
  });
  await region.scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/evaluations-mobile-detail.png" });
  expect(errors).toEqual([]);
});

test("real evaluation scheduler: queued zero-case cancellation and running cleanup", async ({
  page,
}) => {
  test.setTimeout(90000);
  const roots: string[] = [],
    evaluations: string[] = [];
  const workspace = (
    await (await page.request.get("/api/workspaces")).json()
  )[0].workspace_id;
  try {
    for (let index = 0; index < 4; index++) {
      const session = (
        await (
          await page.request.post("/api/sessions", {
            headers: { "Idempotency-Key": crypto.randomUUID() },
            data: { workspace_id: workspace },
          })
        ).json()
      ).session_id;
      const accepted = await page.request.post(
        `/api/sessions/${session}/runs`,
        {
          headers: { "Idempotency-Key": crypto.randomUUID() },
          data: {
            prompt: `队列并发场景 eval-root-${index}`,
            mode: "plan",
            execution_target: "snapshot",
          },
        },
      );
      expect(accepted.status()).toBe(202);
      roots.push((await accepted.json()).run_id);
    }
    await expect
      .poll(
        async () =>
          (await (await page.request.get("/api/queue")).json()).counts
            .active_roots,
      )
      .toBe(4);
    await page.goto("./#/evaluations");
    await expect(
      page.getByRole("button", { name: "运行一次离线自检", exact: true }),
    ).toBeEnabled();
    const accepted = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        /\/api\/projects\/[^/]+\/evaluations$/.test(response.url()),
    );
    await page
      .getByRole("button", { name: "运行一次离线自检", exact: true })
      .click();
    const queued = (await (await accepted).json()).evaluation_id;
    evaluations.push(queued);
    const region = page.getByRole("region", { name: "评测运行详情" });
    await expect(
      region.getByRole("heading", { name: "排队中", exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(region).toContainText(queued);
    await region
      .getByRole("button", { name: "取消评测排队", exact: true })
      .click();
    await expect(
      region.getByRole("heading", { name: "已中断", exact: true }),
    ).toBeVisible();
    const canceled = await (
      await page.request.get(`/api/evaluations/${queued}`)
    ).json();
    expect(canceled.attempted).toBe(0);
    expect(
      canceled.cases.every(
        (value: { status: string }) => value.status === "pending",
      ),
    ).toBe(true);
    expect(
      (await (await page.request.get(`/api/runs/${queued}/usage`)).json())
        .attempt_count,
    ).toBe(0);
    await Promise.all(
      roots.map((run) => page.request.post(`/api/runs/${run}/cancel`)),
    );
    roots.length = 0;
    await expect
      .poll(
        async () =>
          (await (await page.request.get("/api/queue")).json()).counts
            .active_roots,
      )
      .toBe(0);
    await page.getByRole("button", { name: "刷新评测", exact: true }).click();
    await page.getByLabel("任务划分").selectOption("all");
    await expect(
      page.getByRole("button", { name: "运行一次离线自检", exact: true }),
    ).toBeEnabled();
    const runningResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        /\/api\/projects\/[^/]+\/evaluations$/.test(response.url()),
    );
    await page
      .getByRole("button", { name: "运行一次离线自检", exact: true })
      .click();
    const running = (await (await runningResponse).json()).evaluation_id;
    evaluations.push(running);
    await expect(region).toContainText(running);
    await region.getByRole("button", { name: "停止评测", exact: true }).click();
    await expect(
      region.getByRole("heading", { name: "已中断", exact: true }),
    ).toBeVisible({ timeout: 30000 });
    const stopped = await (
      await page.request.get(`/api/evaluations/${running}`)
    ).json();
    expect(stopped.attempted).toBeLessThan(20);
    expect(stopped.remaining).toBeGreaterThan(0);
    await page.reload();
    await expect(region).toContainText(running);
    await expect(
      region.getByRole("heading", { name: "已中断", exact: true }),
    ).toBeVisible();
    await expect
      .poll(
        async () =>
          (await (await page.request.get("/api/queue")).json()).counts
            .active_roots,
      )
      .toBe(0);
  } finally {
    await Promise.all(
      evaluations.map((id) =>
        page.request.post(`/api/evaluations/${id}/cancel`, {
          data: {},
          headers: { "Idempotency-Key": crypto.randomUUID() },
        }),
      ),
    );
    await Promise.all(
      roots.map((run) => page.request.post(`/api/runs/${run}/cancel`)),
    );
  }
});
