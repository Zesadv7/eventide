import { test, expect } from "@playwright/test";

test("capability card preserves unknown facts and verification distinctions after reload", async ({
  page,
}) => {
  await page.goto("./#/settings");
  await page.getByText("模型能力与验证记录", { exact: true }).click();
  const card = page.locator(".capability-card");
  await expect(card).toContainText("目录版本 1");
  await expect(card).toContainText("工具验证：未验证");
  await expect(card).toContainText("上下文 未知");
  const model = await page.getByLabel("默认模型", { exact: true }).inputValue();
  const facts = await (
    await page.request.get(
      `/api/generation/defaults?model=${encodeURIComponent(model)}`,
    )
  ).json();
  expect(facts.capability.verification.tool_verification.status).toBe(
    "not_verified",
  );
  expect(facts.capability.capabilities.context_window).toBeNull();
  await expect(card).toContainText(
    facts.capability.identity.canonical_endpoint,
  );
  await page.reload();
  await page.getByText("模型能力与验证记录", { exact: true }).click();
  await expect(card).toContainText("工具验证：未验证");
  await page.setViewportSize({ width: 320, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
