import { test, expect } from "@playwright/test";

test("delivery survives lost acceptance and refresh, then requires reviewed adoption", async ({
  page,
}) => {
  const keys: string[] = [],
    errors: string[] = [];
  let identity = "";
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/projects/*/deliveries", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (keys.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      identity = (await response.json()).delivery_id;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.goto("./#/deliveries");
  await page.getByLabel("副本名称").fill("browser-delivery");
  await page.getByRole("button", { name: "创建交付副本" }).click();
  await expect(
    page.getByRole("button", { name: "重试原交付请求" }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "重试原交付请求" }).click();
  await expect(page).toHaveURL(new RegExp(`delivery=${identity}`));
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  await page
    .getByLabel("修复目标")
    .fill("交付修复场景：创建已独立验收的结果文件。");
  await page
    .getByLabel("验收命令")
    .fill('test "$(cat delivery-result.txt)" = "independently accepted"');
  await page.getByLabel("累计 Token 软限").fill("200000");
  await page.getByRole("button", { name: "开始有界修复" }).click();
  await expect
    .poll(
      async () => {
        const state = await (
          await page.request.get(`/api/deliveries/${identity}`)
        ).json();
        if (state.repair_status === "stopped") {
          const op = await (
            await page.request.get(
              `/api/delivery-operations/${state.repair.control_run_id}`,
            )
          ).json();
          throw new Error(JSON.stringify(op));
        }
        return state.repair_status;
      },
      { timeout: 30000 },
    )
    .toBe("passed");
  await expect(page.locator("article")).toContainText("验收通过");
  const value = await (
    await page.request.get(`/api/deliveries/${identity}`)
  ).json();
  expect(value.applied).toBe(false);
  expect(value.rounds).toHaveLength(1);
  expect(value.repair.limits.observed_tokens).toBe(200000);
  await page.reload();
  await expect(
    page.getByText("固定 Token 软限：200000", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看验收补丁" }).click();
  await expect(page.locator("pre.patch")).toContainText(
    "independently accepted",
  );
  const adopt = page.getByRole("button", { name: "采纳已检查的补丁" });
  await expect(adopt).toBeDisabled();
  await page.getByLabel("我已检查此补丁，确认采纳到源项目").check();
  await adopt.click();
  await expect(
    page.getByText("补丁已采纳到源项目，未自动提交或推送。", { exact: true }),
  ).toBeVisible();
  const result = await (
    await page.request.get(`/api/deliveries/${identity}`)
  ).json();
  expect(result.applied).toBe(true);
  const session = (
    await (
      await page.request.post("/api/sessions", {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: { workspace_id: result.workspace_id },
      })
    ).json()
  ).session_id;
  const next = await (
    await page.request.post(`/api/sessions/${session}/runs`, {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: {
        prompt: "采纳后继续读取",
        mode: "plan",
        execution_target: "snapshot",
      },
    })
  ).json();
  await expect
    .poll(async () => {
      const run = await (
        await page.request.get(`/api/runs/${next.run_id}`)
      ).json();
      if (run.status === "failed") throw new Error(JSON.stringify(run));
      return run.status;
    })
    .toBe("completed");
  await page.getByRole("link", { name: "查看累计用量与预算" }).click();
  await expect(
    page.getByText(new RegExp(`交付 ${identity}：包含所有轮次`)),
  ).toBeVisible();
  const costs = await (
    await page.request.get(
      `/api/costs?scope_kind=delivery&scope_id=${identity}`,
    )
  ).json();
  expect(costs.usage.attempt_count).toBeGreaterThan(0);
  const detailCount = costs.usage.calls.length * 2;
  await expect(page.locator(".call-details tbody > tr")).toHaveCount(
    detailCount,
  );
  await page.reload();
  await expect(
    page.getByText(new RegExp(`交付 ${identity}：包含所有轮次`)),
  ).toBeVisible();
  await expect(page.locator(".call-details tbody > tr")).toHaveCount(
    detailCount,
  );
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.goBack();
  await page.reload();
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/deliveries-mobile.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
