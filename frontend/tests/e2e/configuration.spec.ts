import { test, expect } from "@playwright/test";

test.afterAll(async ({ request }) => {
  const current = await (await request.get("/api/config/provider")).json();
  const restored = await request.delete("/api/config/provider", {
    data: { expected_config_hash: current.config_hash },
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
  expect(restored.ok()).toBe(true);
});

test("configuration save survives a lost response and reload, then rejects a stale tab", async ({
  page,
  context,
}) => {
  const before = await (await page.request.get("/api/config/provider")).json();
  const keys: string[] = [];
  await page.route("**/api/config/provider", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]!);
    if (keys.length === 1) {
      expect((await route.fetch()).status()).toBe(200);
      await route.abort("failed");
    } else await route.continue();
  });
  await page.goto("./#/settings");
  await expect(page.getByLabel("默认模型", { exact: true })).toHaveValue(
    before.model,
  );
  await page
    .getByLabel("默认模型", { exact: true })
    .fill("configuration-browser-fixture");
  await page
    .getByLabel("API Key", { exact: true })
    .fill("configuration-browser-secret");
  await page.getByRole("button", { name: "保存连接", exact: true }).click();
  await expect(page.getByText(/上次配置请求尚未确认/)).toBeVisible();
  expect(
    await page.evaluate(() =>
      JSON.stringify({ ...sessionStorage, ...localStorage }),
    ),
  ).not.toContain("configuration-browser-secret");
  await page.reload();
  await expect(
    page.getByRole("button", { name: "重试原保存请求", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
  await page
    .getByLabel("API Key", { exact: true })
    .fill("configuration-browser-secret");
  await page
    .getByRole("button", { name: "重试原保存请求", exact: true })
    .click();
  await expect(
    page.getByText(
      "连接配置保存已确认，已读取服务当前配置。尚未发起模型调用。",
      { exact: true },
    ),
  ).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  const other = await context.newPage();
  await other.goto("./#/settings");
  await expect(other.getByLabel("默认模型", { exact: true })).toHaveValue(
    "configuration-browser-fixture",
  );
  await page
    .getByLabel("默认模型", { exact: true })
    .fill("configuration-newer");
  await page.getByRole("button", { name: "保存连接", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await (await page.request.get("/api/config/provider")).json()).model,
    )
    .toBe("configuration-newer");
  await other
    .getByLabel("默认模型", { exact: true })
    .fill("configuration-stale");
  await other.getByRole("button", { name: "保存连接", exact: true }).click();
  await expect(
    other.getByText("配置已变化或服务忙，请刷新配置后重试", { exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get("/api/config/provider")).json()).model,
  ).toBe("configuration-newer");
  await other.close();
});

test("legacy configuration dialog saves through the versioned endpoint", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "配置模型连接", exact: true }).click();
  await expect(page.locator("#model")).not.toHaveValue("");
  await page.locator("#model").fill("legacy-config-fixture");
  await page.locator("#api-key").fill("legacy-config-secret");
  await page.locator("#save-model-config").click();
  await expect(page.locator("#config-message")).toContainText("配置操作已确认");
  expect(
    (await (await page.request.get("/api/config/provider")).json()).model,
  ).toBe("legacy-config-fixture");
  await expect(page.locator("#api-key")).toHaveValue("");
});

test("generation settings preserve zero and explicit default through the real API", async ({
  page,
}) => {
  const provider = await (
    await page.request.get("/api/config/provider")
  ).json();
  const current = await (
    await page.request.get("/api/generation/defaults", {
      params: { model: provider.model },
    })
  ).json();
  expect(
    (
      await page.request.put("/api/generation/defaults", {
        data: {
          model: provider.model,
          generation: {},
          capabilities: { temperature_max: 2 },
          expected_config_hash: current.config_hash,
        },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      })
    ).ok(),
  ).toBe(true);
  await page.goto("./#/settings");
  await expect(
    page.getByRole("button", { name: "保存生成设置", exact: true }),
  ).toBeEnabled();
  await page.locator("#mode-model-temperature").selectOption("value");
  await page.locator('input[aria-label="温度"]').fill("0");
  await page.getByRole("button", { name: "保存生成设置", exact: true }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get("/api/generation/defaults", {
              params: { model: provider.model },
            })
          ).json()
        ).defaults,
    )
    .toEqual({ temperature: 0 });
  await expect(
    page.getByRole("button", { name: "保存生成设置", exact: true }),
  ).toBeEnabled();
  await page.locator("#mode-model-temperature").selectOption("default");
  await page.getByRole("button", { name: "保存生成设置", exact: true }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get("/api/generation/defaults", {
              params: { model: provider.model },
            })
          ).json()
        ).defaults,
    )
    .toEqual({ temperature: null });
});
