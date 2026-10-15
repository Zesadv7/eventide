import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 45000,
  use: {
    baseURL: process.env.EVENTIDE_WEB_URL || "http://127.0.0.1:5173",
    headless: true,
    launchOptions: process.env.EVENTIDE_CHROMIUM
      ? { executablePath: process.env.EVENTIDE_CHROMIUM }
      : {},
  },
  reporter: "list",
});
