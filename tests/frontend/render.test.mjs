import assert from "node:assert/strict";
import test from "node:test";

function fakeElement() {
  return {
    dataset: {},
    innerHTML: "",
    textContent: "",
    querySelectorAll: () => [],
    removeAttribute(name) {
      const key = name.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
      delete this.dataset[key];
    },
  };
}

const nodes = {
  "#main-content": fakeElement(),
  "#task-list": fakeElement(),
  "#task-count": fakeElement(),
  "#workspace-label": fakeElement(),
  "#toast-region": fakeElement(),
};

globalThis.document = {
  activeElement: null,
  querySelector: (selector) => nodes[selector] || null,
};
globalThis.window = {
  getSelection: () => ({ isCollapsed: true, anchorNode: null }),
  scrollY: 0,
};
globalThis.requestAnimationFrame = (callback) => callback();

const [{ render, updateModelSuggestions }, { state }] = await Promise.all([
  import("../../web/modules/render.js"),
  import("../../web/modules/state.js"),
]);

test("home render keeps sidebar, metrics, and escaping behavior", async () => {
  Object.assign(state, {
    page: "home",
    selectedTaskId: null,
    mainDirty: false,
    tasks: [{ id: "task-1", title: "<Unsafe>", status: "running", progress: 25 }],
    repositories: [{ id: "repo-1", alias: "Repo", is_default: true, available: true }],
    settings: {
      planner: { backend: "codex" },
      runs: { active: 2, queued: 3 },
      globalConcurrency: 4,
    },
  });

  await render();

  assert.equal(nodes["#task-count"].textContent, 1);
  assert.equal(nodes["#workspace-label"].textContent, "Repo");
  assert.match(nodes["#task-list"].innerHTML, /&lt;Unsafe&gt;/);
  assert.doesNotMatch(nodes["#task-list"].innerHTML, /<Unsafe>/);
  assert.match(nodes["#task-list"].innerHTML, /class="task-nav-menu"/);
  assert.match(nodes["#task-list"].innerHTML, /data-action="rename-task"/);
  assert.match(nodes["#task-list"].innerHTML, /data-action="delete-task"/);
  assert.match(nodes["#task-list"].innerHTML, /data-task-action-id="task-1"/);
  assert.match(nodes["#task-list"].innerHTML, /aria-label="&lt;Unsafe&gt;的更多操作"/);
  assert.match(nodes["#main-content"].innerHTML, /<strong>2<\/strong>/);
  assert.match(nodes["#main-content"].innerHTML, /<strong>3<\/strong>/);
  assert.match(nodes["#main-content"].innerHTML, /data-action="new-task"/);
});

test("new-task render retains repository and concurrency fields", async () => {
  state.page = "new";
  state.selectedTaskId = null;
  await render();

  assert.match(nodes["#main-content"].innerHTML, /id="task-form"/);
  assert.match(nodes["#main-content"].innerHTML, /name="repositoryId"/);
  assert.match(nodes["#main-content"].innerHTML, /name="workingSubdir"/);
  assert.match(nodes["#main-content"].innerHTML, /name="maxConcurrency"[^>]+max="4"/);
  assert.match(nodes["#main-content"].innerHTML, /value="4"/);
});

test("new-task empty state links directly to repository settings", async () => {
  state.page = "new";
  state.selectedTaskId = null;
  state.repositories = [];
  await render();

  assert.match(nodes["#main-content"].innerHTML, /没有可用仓库/);
  assert.match(nodes["#main-content"].innerHTML, /data-action="settings" data-settings-section="repositories"/);
});

test("settings render uses categorized single-panel layout and omits the obsolete hint", async () => {
  state.page = "settings";
  state.selectedTaskId = null;
  state.settingsSection = "agents";
  state.me = { person: "Tester", credentials: {} };
  state.skill = { content: "# Test skill\n", customized: false };
  state.settings = {
    globalConcurrency: 4,
    tenantRuns: { limit: 1 },
    preferences: {
      planner: { executor: "codex", model: "gpt-6-sol" },
      worker: { executor: "claude", model: "sonnet" },
      verifier: { executor: "codex", model: "retired-model" },
    },
    executors: [
      { id: "codex", name: "Codex", kind: "codex-cli", available: true, models: [{ model: "gpt-6-sol" }] },
      { id: "claude", name: "Claude", kind: "claude-cli", available: true, models: [{ model: "sonnet" }, { model: "opus" }] },
    ],
  };

  await render();

  assert.match(nodes["#main-content"].innerHTML, /data-action="settings-back"/);
  assert.match(nodes["#main-content"].innerHTML, /←/);
  assert.match(nodes["#main-content"].innerHTML, /class="settings-nav"/);
  assert.match(nodes["#main-content"].innerHTML, /Agent 配置/);
  assert.match(nodes["#main-content"].innerHTML, /模型凭证/);
  assert.match(nodes["#main-content"].innerHTML, /Skill 配置/);
  assert.match(nodes["#main-content"].innerHTML, /仓库管理/);
  assert.match(nodes["#main-content"].innerHTML, /规划 Agent/);
  assert.match(nodes["#main-content"].innerHTML, /执行模型/);
  assert.match(nodes["#main-content"].innerHTML, /<select name="plannerModel"[^>]*>/);
  assert.match(nodes["#main-content"].innerHTML, />CLI 默认模型<\/option>/);
  assert.match(nodes["#main-content"].innerHTML, />gpt-6-sol<\/option>/);
  assert.match(nodes["#main-content"].innerHTML, />retired-model（历史配置）<\/option>/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /<input name="plannerModel"/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /id="skill-settings-form"/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /class="settings-body credential-form"/);

  state.settingsSection = "credentials";
  await render();
  assert.match(nodes["#main-content"].innerHTML, /默认使用你空间中的 Codex CLI 登录/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /id="agent-settings-form"/);

  state.settingsSection = "skills";
  await render();
  assert.match(nodes["#main-content"].innerHTML, /id="skill-settings-form"/);
  assert.match(nodes["#main-content"].innerHTML, /# Test skill/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /id="repository-form"/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /fastlab\.env/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /重启后生效/);

  state.settingsSection = "repositories";
  state.repositoryManagement = { mode: "managed", acceptsPath: false, acceptsCloneUrl: true };
  state.repositories = [{
    id: "repo-1", alias: "Managed", path: "/srv/private/tenant/repo-1",
    is_default: true, available: true, source: "clone", deletable: true,
  }];
  await render();
  assert.match(nodes["#main-content"].innerHTML, /name="cloneUrl"/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /留空则创建空仓库|不要在 URL 中填写 Token/);
  assert.match(nodes["#main-content"].innerHTML, /从远程仓库克隆/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /仅移除记录|data-action="delete-repository"/);
  assert.match(nodes["#main-content"].innerHTML, /永久删除仓库/);
  assert.match(nodes["#main-content"].innerHTML, /data-action="delete-managed-repository"/);
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /name="path"/);
  assert.ok(
    nodes["#main-content"].innerHTML.indexOf('id="repository-form"')
      < nodes["#main-content"].innerHTML.indexOf('class="repository-list"'),
  );
  assert.doesNotMatch(nodes["#main-content"].innerHTML, /\/srv\/private/);
});

test("changing an Agent replaces the strict model options and selects the CLI default", () => {
  const select = { innerHTML: "", value: "old-model" };
  updateModelSuggestions(select, "claude");

  assert.equal(select.value, "");
  assert.match(select.innerHTML, /CLI 默认模型/);
  assert.match(select.innerHTML, /sonnet/);
  assert.match(select.innerHTML, /opus/);
  assert.doesNotMatch(select.innerHTML, /gpt-6-sol/);
  assert.doesNotMatch(select.innerHTML, /old-model/);
});
