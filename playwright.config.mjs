import { defineConfig } from "@playwright/test";

const python = process.env.FASTLAB_TEST_PYTHON || ".venv/bin/python";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 20_000,
  fullyParallel: false,
  workers: 1,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4199",
    browserName: "chromium",
    headless: true,
  },
  webServer: {
    command: `${python} deploy/e2e_server.py`,
    url: "http://127.0.0.1:4199/api/health",
    reuseExistingServer: false,
    timeout: 20_000,
  },
});
