import { expect, type Page } from "@playwright/test";

/** Use the visible workbench disclosure, rather than bypassing navigation. */
export async function openWorkbenchPage(page: Page, name: string) {
  const tools = page.locator("details.workspace-tools");
  if ((await tools.getAttribute("open")) === null)
    await tools.locator("summary").click();
  const link = page
    .getByRole("navigation", { name: "工作台导航", exact: true })
    .getByRole("link", { name, exact: true });
  await expect(link).toBeVisible();
  await link.click();
}
