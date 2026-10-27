import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  fullyParallel: false,
  workers: 1,
  reporter: [
    ["list"],
    ["json", { outputFile: "../data/browser-test-results.json" }],
  ],
  use: {
    baseURL: "http://127.0.0.1:8018",
    viewport: { width: 1440, height: 960 },
    launchOptions: {
      executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
      args: ["--no-proxy-server"],
    },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  outputDir: "../data/browser-test-artifacts",
});
