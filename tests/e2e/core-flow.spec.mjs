import { expect, test } from "@playwright/test";


function plannedTask() {
  return {
    id: "task-created",
    title: "E2E task",
    goal: "Exercise the browser workflow",
    status: "awaiting_approval",
    progress: 0,
    repository_alias: "fixture",
    max_concurrency: 4,
    role_settings: { verifier: {} },
    plan: {
      summary: "Browser-generated plan",
      acceptance: [{ id: "A1", criterion: "The flow renders" }],
    },
    subtasks: [{
      id: "subtask-1",
      plan_key: "S1",
      title: "Implement fixture",
      instructions: "Keep the browser flow deterministic.",
      dependencies: [],
      weight: 1,
      attempt: 0,
      status: "pending",
      executor: "codex",
      executor_snapshot: { name: "Codex" },
    }],
    events: [],
    documents: {
      "task.md": "# E2E task\n",
      "progress.md": "# 任务进度\n",
      "acceptance.md": "# 验收标准\n",
    },
  };
}


async function mockControlPlane(page) {
  let created = false;
  let submitted = null;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (payload, status = 200) => route.fulfill({
      status,
      contentType: "application/json; charset=utf-8",
      body: JSON.stringify(payload),
    });
    if (path === "/api/agents") return json({ agents: [] });
    if (path === "/api/health") return json({ ok: true });
    if (path === "/api/me") return json({
      person: "E2E User",
      spaceReady: true,
      credentials: { openai: { configured: true }, anthropic: { configured: false } },
    });
    if (path === "/api/repositories") return json({ repositories: [{
      id: "repo-1", alias: "fixture", path: "opaque-id", is_default: true,
      available: true, source: "created", managed: true, deletable: true,
    }], management: { mode: "managed", acceptsPath: false, acceptsCloneUrl: true } });
    if (path === "/api/settings/executors") return json({
      planner: { backend: "codex" },
      preferences: {
        planner: { executor: "codex", model: "" },
        worker: { executor: "codex", model: "" },
        verifier: { executor: "codex", model: "" },
      },
      executors: [{ id: "codex", name: "Codex", kind: "codex-cli", available: true, models: [{ model: "gpt-6-sol" }] }],
      globalConcurrency: 4,
      runs: { active: 0, queued: 0, limit: 4 },
      tenantRuns: { active: 0, queued: 0, limit: 1 },
    });
    if (path === "/api/settings/skill") return json({
      content: "# E2E orchestration skill\n",
      customized: true,
    });
    if (path === "/api/tasks" && request.method() === "GET") {
      return json({ tasks: created ? [{ id: "task-created", title: "E2E task", status: "awaiting_approval", progress: 0 }] : [] });
    }
    if (path === "/api/tasks" && request.method() === "POST") {
      submitted = request.postDataJSON();
      created = true;
      return json({ id: "task-created" }, 202);
    }
    if (path === "/api/tasks/task-created") return json(plannedTask());
    return json({ error: `Unhandled test route: ${path}` }, 404);
  });
  return { submitted: () => submitted };
}


test("loads the dashboard and submits the create-task workflow", async ({ page }) => {
  const requests = await mockControlPlane(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
  await expect(page.locator("#workspace-label")).toHaveText("fixture");

  await page.locator(".new-task-button").click();
  await expect(page.getByRole("heading", { name: "创建任务" })).toBeVisible();
  await page.locator('input[name="title"]').fill("E2E task");
  await page.locator('textarea[name="goal"]').fill("Exercise the browser workflow");
  await page.getByRole("button", { name: "生成计划" }).click();

  await expect(page.getByRole("heading", { name: "E2E task" })).toBeVisible();
  await expect(page.getByText("Implement fixture")).toBeVisible();
  await page.locator('[data-task-panel="subtask-subtask-1"] > summary').click();
  await expect(page.locator('[data-subtask-model="subtask-1"]')).toBeVisible();
  await expect(page.locator('[data-subtask-model="subtask-1"] option')).toHaveText([
    "CLI 默认模型",
    "gpt-6-sol",
  ]);
  expect(requests.submitted()).toMatchObject({
    repositoryId: "repo-1",
    title: "E2E task",
    goal: "Exercise the browser workflow",
    maxConcurrency: 4,
  });
});


test("uses the in-app categorized settings view and returns to its origin", async ({ page }) => {
  await mockControlPlane(page);
  await page.goto("/");
  await page.locator(".settings-button").click();
  await expect(page.getByRole("heading", { name: "设置" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "设置分类" })).toBeVisible();
  await expect(page.locator("#agent-settings-form")).toBeVisible();
  await expect(page.locator("#skill-settings-form")).toHaveCount(0);
  await expect(page.getByText("我的并发")).toBeVisible();

  await page.getByRole("button", { name: /模型凭证/ }).click();
  await expect(page.getByText("E2E User")).toBeVisible();
  await expect(page.getByText("未填写 API Key 时，默认使用你空间中的 Codex CLI 登录。")).toBeVisible();
  await expect(page.locator("#agent-settings-form")).toHaveCount(0);

  await page.getByRole("button", { name: /Skill 配置/ }).click();
  await expect(page.locator("#skill-settings-form")).toBeVisible();
  await expect(page.locator("#repository-form")).toHaveCount(0);

  await page.getByRole("button", { name: /仓库管理/ }).click();
  await expect(page.locator("#repository-form")).toBeVisible();
  await expect(page.locator('input[name="cloneUrl"]')).toBeVisible();
  await expect(page.locator('input[name="path"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "仅移除记录" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "永久删除仓库" })).toBeVisible();
  await page.getByRole("button", { name: "返回之前的页面" }).click();
  await expect(page.getByRole("heading", { name: "任务", exact: true })).toBeVisible();
});


test("creates and permanently deletes a managed repository against the real server", async ({ page }) => {
  await page.goto("/");
  await page.locator(".settings-button").click();
  await page.getByRole("button", { name: /仓库管理/ }).click();
  await expect(page.locator('input[name="path"]')).toHaveCount(0);
  await page.locator('input[name="alias"]').fill("browser-repo");
  await page.getByRole("button", { name: "创建仓库", exact: true }).click();
  const row = page.locator(".repository-row").filter({ hasText: "browser-repo" });
  await expect(row).toBeVisible();
  const repositories = await (await page.request.get("/api/repositories")).json();
  expect(repositories.management.mode).toBe("managed");
  expect(repositories.repositories[0]).toMatchObject({ managed: true, has_commit: true });
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "永久删除仓库" }).click();
  await expect(row).toHaveCount(0);
  expect((await (await page.request.get("/api/repositories")).json()).repositories).toEqual([]);
});
