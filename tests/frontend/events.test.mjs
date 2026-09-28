import assert from "node:assert/strict";
import test from "node:test";

const main = {
  dataset: {},
  contains: () => true,
  querySelectorAll: () => [],
  removeAttribute: () => {},
};
const inert = {
  dataset: {},
  innerHTML: "",
  textContent: "",
  append: () => {},
  querySelectorAll: () => [],
  removeAttribute: () => {},
};

globalThis.document = {
  activeElement: null,
  createElement: () => ({ remove: () => {} }),
  querySelector(selector) {
    return selector === "#main-content" ? main : inert;
  },
};
globalThis.window = {
  getSelection: () => ({ isCollapsed: true, anchorNode: null }),
  scrollY: 0,
  scrollTo: () => {},
};
globalThis.requestAnimationFrame = (callback) => callback();
globalThis.CSS = { escape: (value) => String(value) };

const [{ bindEvents }, { state }] = await Promise.all([
  import("../../web/modules/events.js"),
  import("../../web/modules/state.js"),
]);

test("bindEvents keeps delegated input, change, submit, and click listeners", () => {
  const listeners = [];
  const documentRef = {
    addEventListener: (type, listener) => listeners.push({ type, listener }),
    querySelector: () => null,
  };

  bindEvents(documentRef);

  assert.deepEqual(listeners.map(({ type }) => type), [
    "input",
    "change",
    "change",
    "submit",
    "click",
  ]);

  state.mainDirty = false;
  listeners[0].listener({ target: { matches: () => true } });
  assert.equal(state.mainDirty, true);
});

test("sidebar menu actions target their own task without leaving the selected task", async () => {
  const listeners = [];
  const requests = [];
  const selectedTask = {
    id: "selected-task",
    title: "Selected",
    goal: "Keep viewing this task",
    status: "awaiting_approval",
    progress: 0,
    repository_alias: "Repo",
    max_concurrency: 1,
    subtasks: [],
    role_settings: { verifier: {} },
    plan: { acceptance: [] },
    events: [],
    documents: {},
  };
  const payloads = {
    "/api/tasks": { tasks: [selectedTask] },
    "/api/repositories": { repositories: [] },
    "/api/settings/executors": { executors: [], runs: {} },
    "/api/health": { ok: true },
    "/api/me": { person: "local" },
    "/api/tasks/selected-task": selectedTask,
  };
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    const payload = options.method === "DELETE" ? { ok: true } : payloads[url];
    return { ok: true, status: 200, json: async () => payload };
  };
  globalThis.setTimeout = () => 0;
  window.confirm = () => true;
  window.prompt = () => "Renamed sidebar task";
  state.page = "task";
  state.selectedTaskId = selectedTask.id;
  state.tasks = [selectedTask, { id: "sidebar-task", title: "Sidebar task" }];
  state.taskViews = { "sidebar-task": { pageY: 10 } };
  const documentRef = {
    addEventListener: (type, listener) => listeners.push({ type, listener }),
    querySelector: () => null,
  };
  bindEvents(documentRef);
  const click = listeners.find(({ type }) => type === "click").listener;
  const actionButton = { dataset: {} };
  const target = {
    closest(selector) {
      if (selector === "[data-action]") return actionButton;
      return null;
    },
  };

  try {
    actionButton.dataset = { action: "rename-task", taskActionId: "sidebar-task" };
    await click({ target });
    actionButton.dataset = { action: "delete-task", taskActionId: "sidebar-task" };
    await click({ target });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
  }

  assert.ok(requests.some(({ url, options }) => url === "/api/tasks/sidebar-task"
    && options.method === "PUT"
    && options.body === JSON.stringify({ title: "Renamed sidebar task" })));
  assert.ok(requests.some(({ url, options }) => url === "/api/tasks/sidebar-task" && options.method === "DELETE"));
  assert.equal(state.selectedTaskId, "selected-task");
  assert.equal(state.page, "task");
  assert.equal(state.taskViews["sidebar-task"], undefined);
});

test("settings navigation remembers its origin and protects unsaved changes", async () => {
  const listeners = [];
  const documentRef = {
    addEventListener: (type, listener) => listeners.push({ type, listener }),
    querySelector: () => null,
  };
  bindEvents(documentRef);
  const click = listeners.find(({ type }) => type === "click").listener;
  const button = { dataset: {} };
  const target = {
    closest(selector) {
      if (selector === "[data-action]") return button;
      return null;
    },
  };
  const originalConfirm = window.confirm;
  const originalFetch = globalThis.fetch;
  const originTask = {
    id: "task-origin",
    title: "Origin task",
    goal: "Return here",
    status: "awaiting_approval",
    progress: 0,
    repository_alias: "Repo",
    max_concurrency: 1,
    subtasks: [],
    role_settings: { verifier: {} },
    plan: { acceptance: [] },
    events: [],
    documents: {},
  };
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => originTask });
  state.page = "task";
  state.selectedTaskId = "task-origin";
  state.settingsSection = "agents";
  state.settingsOrigin = null;
  state.mainDirty = false;
  state.tasks = [];
  state.repositories = [];
  state.settings = { executors: [], preferences: {}, runs: {} };
  state.me = { person: "Tester", credentials: {} };
  state.skill = { content: "# User skill", customized: true };

  button.dataset = { action: "settings" };
  await click({ target });
  assert.deepEqual(state.settingsOrigin, { page: "task", selectedTaskId: "task-origin" });
  assert.equal(state.page, "settings");
  assert.equal(state.selectedTaskId, null);

  window.confirm = () => false;
  state.mainDirty = true;
  button.dataset = { action: "settings-section", settingsSection: "skills" };
  await click({ target });
  assert.equal(state.settingsSection, "agents");
  assert.equal(state.mainDirty, true);

  window.confirm = () => true;
  await click({ target });
  assert.equal(state.settingsSection, "skills");
  assert.equal(state.mainDirty, false);

  button.dataset = { action: "settings-back" };
  await click({ target });
  assert.equal(state.page, "task");
  assert.equal(state.selectedTaskId, "task-origin");
  assert.equal(state.settingsOrigin, null);
  window.confirm = originalConfirm;
  globalThis.fetch = originalFetch;
});

test("add repository opens the repository settings section and returns to task creation", async () => {
  const listeners = [];
  const documentRef = {
    addEventListener: (type, listener) => listeners.push({ type, listener }),
    querySelector: () => null,
  };
  bindEvents(documentRef);
  const click = listeners.find(({ type }) => type === "click").listener;
  const button = { dataset: { action: "settings", settingsSection: "repositories" } };
  const target = {
    closest(selector) {
      if (selector === "[data-action]") return button;
      return null;
    },
  };
  state.page = "new";
  state.selectedTaskId = null;
  state.settingsSection = "agents";
  state.settingsOrigin = null;
  state.mainDirty = false;
  state.repositories = [];
  state.settings = { executors: [], preferences: {}, runs: {} };

  await click({ target });
  assert.equal(state.page, "settings");
  assert.equal(state.settingsSection, "repositories");
  assert.deepEqual(state.settingsOrigin, { page: "new", selectedTaskId: null });

  button.dataset = { action: "settings-back" };
  await click({ target });
  assert.equal(state.page, "new");
  assert.equal(state.settingsOrigin, null);
});

test("managed repository form sends a name and Git URL without a server path", async () => {
  const listeners = [];
  const requests = [];
  const documentRef = {
    addEventListener: (type, listener) => listeners.push({ type, listener }),
    querySelector: () => null,
  };
  const originalFetch = globalThis.fetch;
  const OriginalFormData = globalThis.FormData;
  const originalSetTimeout = globalThis.setTimeout;
  globalThis.FormData = class {
    get(name) {
      return { alias: "project", cloneUrl: "https://example.com/project.git" }[name] ?? null;
    }
  };
  globalThis.setTimeout = () => 0;
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    const payloads = {
      "/api/tasks": { tasks: [] },
      "/api/repositories": {
        repositories: [],
        management: { mode: "managed", acceptsPath: false, acceptsCloneUrl: true },
      },
      "/api/settings/executors": { executors: [], preferences: {}, runs: {} },
      "/api/health": { ok: true },
      "/api/me": { person: "Tester", credentials: {} },
    };
    const payload = options.method === "POST" ? { id: "repo-1" } : payloads[url];
    return { ok: true, status: options.method === "POST" ? 201 : 200, json: async () => payload };
  };
  state.page = "settings";
  state.settingsSection = "repositories";
  state.repositoryManagement = { mode: "managed", acceptsPath: false, acceptsCloneUrl: true };
  const form = { id: "repository-form", reset() {}, matches: () => false };
  bindEvents(documentRef);
  const submit = listeners.find(({ type }) => type === "submit").listener;

  try {
    await submit({ preventDefault() {}, target: form });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.FormData = OriginalFormData;
    globalThis.setTimeout = originalSetTimeout;
  }

  const request = requests.find(({ url, options }) => (
    url === "/api/repositories" && options.method === "POST"
  ));
  assert.ok(request);
  assert.deepEqual(JSON.parse(request.options.body), {
    alias: "project",
    cloneUrl: "https://example.com/project.git",
  });
  assert.doesNotMatch(request.options.body, /path/i);
});

test("permanent repository deletion uses the managed endpoint after confirmation", async () => {
  const listeners = [];
  const requests = [];
  const documentRef = {
    addEventListener: (type, listener) => listeners.push({ type, listener }),
    querySelector: () => null,
  };
  const originalFetch = globalThis.fetch;
  const originalConfirm = window.confirm;
  const originalSetTimeout = globalThis.setTimeout;
  window.confirm = () => true;
  globalThis.setTimeout = () => 0;
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    const payloads = {
      "/api/tasks": { tasks: [] },
      "/api/repositories": {
        repositories: [],
        management: { mode: "managed", acceptsPath: false, acceptsCloneUrl: true },
      },
      "/api/settings/executors": { executors: [], preferences: {}, runs: {} },
      "/api/health": { ok: true },
      "/api/me": { person: "Tester", credentials: {} },
    };
    const payload = options.method === "DELETE"
      ? { ok: true, deletedFiles: true }
      : payloads[url];
    return { ok: true, status: 200, json: async () => payload };
  };
  state.page = "settings";
  state.settingsSection = "repositories";
  state.repositoryManagement = { mode: "managed", acceptsPath: false, acceptsCloneUrl: true };
  state.repositories = [{ id: "abc123", alias: "project", deletable: true }];
  bindEvents(documentRef);
  const click = listeners.find(({ type }) => type === "click").listener;
  const button = {
    dataset: { action: "delete-managed-repository", repositoryId: "abc123" },
    closest: () => null,
  };
  const target = {
    closest(selector) {
      if (selector === "[data-action]") return button;
      return null;
    },
  };

  try {
    await click({ target });
  } finally {
    globalThis.fetch = originalFetch;
    window.confirm = originalConfirm;
    globalThis.setTimeout = originalSetTimeout;
  }

  assert.ok(requests.some(({ url, options }) => (
    url === "/api/repositories/abc123/managed" && options.method === "DELETE"
  )));
});
