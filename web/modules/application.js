import { api } from "./api.js";
import { canAutoRenderMain, main, render, renderSidebar } from "./render.js";
import { escapeHtml } from "./presentation.js";
import { state } from "./state.js";

export async function refresh({ keepPage = true, renderMain = true, respectEditing = false } = {}) {
  const [tasks, repositoryPayload, settings, health, me, library] = await Promise.all([
    api("/api/tasks"),
    api("/api/repositories"),
    api("/api/settings/executors"),
    api("/api/health"),
    api("/api/me"),
    api("/api/agents"),
  ]);
  state.tasks = tasks.tasks;
  state.repositories = repositoryPayload.repositories;
  state.repositoryManagement = repositoryPayload.management || {
    mode: "managed", acceptsPath: false, acceptsCloneUrl: true,
  };
  state.settings = settings;
  state.health = health;
  state.me = me;
  state.agents = library.agents || [];
  if (!keepPage) {
    state.page = "home";
    state.selectedTaskId = null;
  }
  if (renderMain && (!respectEditing || canAutoRenderMain())) await render();
  else renderSidebar();
}

export async function start() {
  try {
    await refresh({ keepPage: false });
    state.timer = setInterval(() => refresh({ respectEditing: true }).catch(() => {}), 2500);
  } catch (error) {
    main.innerHTML = `<section class="empty-state"><h2>FastLab 暂时不可用</h2><p>${escapeHtml(error.message)}</p></section>`;
  }
}
