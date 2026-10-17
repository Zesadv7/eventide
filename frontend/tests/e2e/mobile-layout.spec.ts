import { test, expect } from "@playwright/test";

for (const route of [
  "projects",
  "deliveries",
  "evaluations",
  "imports",
  "worktrees",
  "history",
  "usage",
]) {
  test(`mobile layout: ${route} remains usable while resizing`, async ({
    page,
  }) => {
    await page.goto(`./#/${route}`);
    await expect(page.locator("main > .page")).toBeVisible();
    await page.setViewportSize({ width: 320, height: 844 });
    const measurements = await page.evaluate(async () => {
      const samples = [];
      for (let frame = 0; frame < 20; frame++) {
        samples.push({
          width: document.documentElement.scrollWidth,
          viewport: innerWidth,
          offenders: Array.from(document.querySelectorAll("main, main *"))
            .map((el) => ({
              tag: el.tagName,
              class: el.className,
              right: el.getBoundingClientRect().right,
              width: el.getBoundingClientRect().width,
            }))
            .filter((el) => el.right > innerWidth + 1)
            .slice(0, 12),
        });
        await new Promise(requestAnimationFrame);
      }
      return samples;
    });
    expect(
      measurements.filter((sample) => sample.width > sample.viewport),
    ).toEqual([]);
  });
}
