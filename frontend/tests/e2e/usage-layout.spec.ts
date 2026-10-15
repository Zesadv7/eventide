import { test, expect } from "@playwright/test";
import { costs, ledger } from "../fixtures";

test("usage layout fixture: repeated-failure identities wrap on a 320px screen", async ({
  page,
}) => {
  const run = `run_${"a".repeat(32)}`;
  await page.route("**/api/costs?**", (route) =>
    route.fulfill({
      json: {
        ...costs,
        usage: {
          ...ledger,
          calls: [1, 2].map((index) => ({
            ...ledger.calls[0]!,
            run_id: run,
            attempt_id: `attempt-${index}`,
          })),
          attempt_count: 2,
          unknown_attempts: 2,
          incomplete_attempts: 2,
        },
      },
    }),
  );
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto("./#/usage");
  await page.getByRole("button", { name: "每日（UTC）", exact: true }).click();
  await page.locator(".usage-failures summary").click();
  await expect(page.locator(".usage-failures code")).toHaveText(run);
  await expect(page.locator(".sidebar")).not.toBeInViewport();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(320);
  await page.screenshot({
    path: "test-results/usage-failures-320.png",
    fullPage: true,
  });
});
