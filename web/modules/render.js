import { agentOptions, libraryView, assignment, runHistory } from "./agent-library.js";
import { api } from "./api.js";
import {
  escapeHtml,
  logPresentation,
  plannerLabel,
  visibleLogEvents,
} from "./presentation.js";
import { state, statusLabels } from "./state.js";

export const main = document.querySelector("#main-content");
const taskList = document.querySelector("#task-list");
const taskCount = document.querySelector("#task-count");
const workspaceLabel = document.querySelector("#workspace-label");

export function toast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  document.querySelector("#toast-region").append(node);
  setTimeout(() => node.remove(), 3600);
}

function executorOptions(selected = "") {
  return (state.settings?.executors || []).map((item) =>
    `<option value="${item.id}" ${item.id === selected ? "selected" : ""} ${item.available ? "" : "disabled"}>${escapeHtml(item.name)}${item.available ? "" : " · 不可用"}</option>`
  ).join("");
}

function executorModels(executor) {
  const item = (state.settings?.executors || []).find((value) => value.id === executor);
  return [...new Set((item?.models || [])
    .map((value) => String(value.model || value.slug || value.id || "").trim())
    .filter(Boolean))];
}

function modelSelectOptions(executor, selected = "") {
  const current = String(selected || "").trim();
  const models = executorModels(executor);
  const historical = current && !models.includes(current)
    ? `<option value="${escapeHtml(current)}" selected>${escapeHtml(current)}（历史配置）</option>`
    : "";
  return `<option value="" ${current ? "" : "selected"}>CLI 默认模型</option>
    ${models.map((model) => `<option value="${escapeHtml(model)}" ${model === current ? "selected" : ""}>${escapeHtml(model)}</option>`).join("")}
    ${historical}`;
}

function modelPicker(kind, id, executor, selected = "") {
  const attribute = kind === "verifier" ? 'id="verifier-model"' : `data-subtask-model="${escapeHtml(id)}"`;
  return `<select ${attribute} aria-label="模型">${modelSelectOptions(executor, selected)}</select>`;
}

function settingsModelPicker(role, preference = {}) {
  return `<select name="${role}Model" data-settings-model="${role}" aria-label="模型">${modelSelectOptions(preference.executor, preference.model)}</select>`;
}

export function updateModelSuggestions(select, executor) {
  if (!select) return;
  select.innerHTML = modelSelectOptions(executor);
  select.value = "";
}

function rememberTaskView() {
  const taskId = main.dataset.taskViewId;
  if (!taskId) return;
  const view = state.taskViews[taskId] || {};
  const panels = {};
  main.querySelectorAll("details[data-task-panel]").forEach((panel) => {
    panels[panel.dataset.taskPanel] = panel.open;
  });
  const scrolls = {};
  main.querySelectorAll("[data-task-scroll]").forEach((node) => {
    scrolls[node.dataset.taskScroll] = {
      top: node.scrollTop,
      left: node.scrollLeft,
      atTop: node.scrollTop <= 8,
      bottom: Math.max(0, node.scrollHeight - node.clientHeight - node.scrollTop),
    };
  });
  state.taskViews[taskId] = {
    ...view, panels, scrolls, pageY: window.scrollY,
  };
}

function taskPanelOpen(taskId, panel) {
  return state.taskViews[taskId]?.panels?.[panel] ? "open" : "";
}

function restoreTaskView(taskId) {
  const view = state.taskViews[taskId];
  if (!view) return;
  main.querySelectorAll("[data-task-scroll]").forEach((node) => {
    const saved = view.scrolls?.[node.dataset.taskScroll];
    if (!saved) return;
    if (node.dataset.taskScroll === "logs" && !saved.atTop) {
      node.scrollTop = Math.max(0, node.scrollHeight - node.clientHeight - saved.bottom);
    } else {
      node.scrollTop = saved.atTop ? 0 : saved.top;
    }
    node.scrollLeft = saved.left;
  });
  requestAnimationFrame(() => {
    if (main.dataset.taskViewId === taskId) window.scrollTo(0, view.pageY || 0);
  });
}

export function leaveTaskView() {
  main.removeAttribute("data-task-view-id");
}

const sidebarTaskActions = Object.freeze([
  { action: "rename-task", label: "重命名任务" },
  { action: "delete-task", label: "删除任务", danger: true },
]);

function taskActionMenu(task) {
  const taskId = escapeHtml(task.id);
  return `<details class="task-nav-menu" data-task-menu-id="${taskId}" ${task.id === state.openTaskMenuId ? "open" : ""}>
    <summary class="task-nav-more" aria-label="${escapeHtml(task.title)}的更多操作" title="更多操作">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>
    </summary>
    <div class="task-nav-popover" role="menu">
      ${sidebarTaskActions.map((item) => `<button class="task-menu-item ${item.danger ? "danger" : ""}" type="button" role="menuitem" data-action="${item.action}" data-task-action-id="${taskId}">${item.label}</button>`).join("")}
    </div>
  </details>`;
}

export function renderSidebar() {
  taskCount.textContent = state.tasks.length;
  const current = state.repositories.find((item) => item.is_default);
  workspaceLabel.textContent = current ? `${current.alias}${current.available ? "" : " · 不可用"}` : "尚未登记";
  taskList.innerHTML = state.tasks.length ? state.tasks.map((task) => `
    <div class="task-nav-row">
      <button class="task-nav-item ${task.id === state.selectedTaskId ? "active" : ""}" type="button" data-task-id="${escapeHtml(task.id)}">
        <span class="nav-status-dot ${escapeHtml(task.status)}"></span>
        <span class="task-nav-copy"><strong>${escapeHtml(task.title)}</strong><span>${escapeHtml(statusLabels[task.status] || task.status)}</span></span>
        <span class="nav-progress">${task.progress}%</span>
      </button>
      ${taskActionMenu(task)}
    </div>`).join("") : '<div class="task-list-empty">暂无任务</div>';
}

function renderHome() {
  leaveTaskView();
  const ready = state.repositories.some((item) => item.available);
  const planner = state.settings?.planner || {};
  main.innerHTML = `
    <section class="page-heading">
      <div><h2 class="page-title">任务</h2></div>
      <div class="page-actions">
        <button class="primary-button" type="button" data-action="new-task" ${ready ? "" : "disabled"}>创建任务</button>
        ${ready ? "" : '<button class="quiet-button" type="button" data-action="settings" data-settings-section="repositories">添加仓库</button>'}
      </div>
    </section>
    <section class="metric-grid">
      <div class="metric"><span class="metric-label">规划</span><strong>${plannerLabel(planner)}</strong></div>
      <div class="metric"><span class="metric-label">执行</span><strong>Codex / Claude</strong></div>
      <div class="metric"><span class="metric-label">运行</span><strong>${state.settings?.runs?.active || 0}</strong></div>
      <div class="metric"><span class="metric-label">排队</span><strong>${state.settings?.runs?.queued || 0}</strong></div>
    </section>`;
}

function renderCreate() {
  leaveTaskView();
  const planner = state.settings?.planner || {};
  const available = state.repositories.filter((item) => item.available);
  if (!available.length) {
    main.innerHTML = '<section class="empty-state"><h2>没有可用仓库</h2><button class="primary-button" data-action="settings" data-settings-section="repositories">添加仓库</button></section>';
    return;
  }
  main.innerHTML = `
    <section class="page-heading"><div><h2 class="page-title">创建任务</h2></div></section>
    <form class="panel task-form" id="task-form">
      <div class="field"><label>仓库</label><select name="repositoryId" required>${available.map((item) => `<option value="${item.id}" ${item.is_default ? "selected" : ""}>${escapeHtml(item.alias)}</option>`).join("")}</select></div>
      <div class="field"><label>目录 <span class="field-hint">可选</span></label><input name="workingSubdir" placeholder="例如 web"><span class="field-note">仓库内的相对目录</span></div>
      <div class="field full"><label>标题 <span class="field-hint">可选</span></label><input name="title" maxlength="80" placeholder="一句话标题"></div>
      <div class="field full"><label>目标</label><textarea name="goal" rows="7" required placeholder="要完成什么？"></textarea></div>
      <div class="field full"><label>限制 <span class="field-hint">可选</span></label><textarea name="constraints" rows="3" placeholder="不能修改什么？"></textarea></div>
      <div class="field"><label>并发数</label><input name="maxConcurrency" type="number" min="1" max="${state.settings?.globalConcurrency || 4}" value="${Math.min(4, state.settings?.globalConcurrency || 4)}" required></div>
      ${[["planner", "规划 Agent"], ["worker", "默认执行 Agent"], ["verifier", "验收 Agent"]].map(([r, n]) => `<div class="field"><label>${n}</label><select name="${r}Agent">${agentOptions(r)}</select></div>`).join("")}
      <div class="form-actions full"><button class="primary-button" type="submit">生成计划</button></div>
    </form>`;
}

function subtaskCard(task, item) {
  const editable = task.status === "awaiting_approval" && !item.attempt;
  const retryable = ["failed", "blocked", "cancelled"].includes(item.status);
  const revisable = item.status === "succeeded"
    && ["needs_attention", "failed", "cancelled"].includes(task.status)
    && !task.cleaned_at && item.worktree;
  const resumable = !task.cleaned_at && item.worktree && item.session_id
    && !["running", "pending"].includes(item.status);
  const snap = item.executor_snapshot || {};
  const selectedModel = item.model || snap.model || "";
  const restartAction = retryable ? "retry" : "revise";
  const restartTitle = retryable ? "重新执行" : "带新要求重跑";
  const restartButton = retryable ? "确认并重试" : "确认修改并重跑";
  return `<details class="subtask-card" data-task-panel="subtask-${escapeHtml(item.id)}" ${taskPanelOpen(task.id, `subtask-${item.id}`)}>
    <summary class="subtask-heading"><span class="subtask-key">${item.plan_key}</span><h4>${escapeHtml(item.title)}</h4><span class="status-pill ${item.status}">${escapeHtml(statusLabels[item.status] || item.status)}</span></summary>
    <div class="subtask-body">
      ${assignment(task, "worker", item)}
      ${editable ? `<section class="subtask-action-panel plan-editor">
        <header class="subtask-action-heading"><div><strong>执行前编辑</strong><span>保存不会启动任务</span></div><span class="edit-badge">可编辑</span></header>
        <label class="subtask-field">简介<input data-subtask-title="${escapeHtml(item.id)}" maxlength="80" value="${escapeHtml(item.title)}"></label>
        <label class="subtask-field">具体要求<textarea data-subtask-instructions="${escapeHtml(item.id)}" rows="5">${escapeHtml(item.instructions)}</textarea></label>
        <div class="dispatch-fields">
          <label>执行器<select data-subtask-executor="${escapeHtml(item.id)}">${executorOptions(item.executor)}</select></label>
          <label>模型${modelPicker("subtask", item.id, item.executor, selectedModel)}</label>
        </div>
        <div class="subtask-action-footer"><span>依赖关系和验收标准请使用“调整计划”。</span><button class="quiet-button" type="button" data-action="save-subtask" data-subtask-id="${escapeHtml(item.id)}">保存子任务</button></div>
      </section>` : `<p class="subtask-instructions">${escapeHtml(item.instructions)}</p>`}
      <div class="subtask-meta"><span>依赖：${item.dependencies.length ? item.dependencies.join(", ") : "无"}</span><span>权重：${item.weight}</span><span>执行器：${escapeHtml(snap.name || item.executor || "待分配")}</span><span>模型：${escapeHtml(selectedModel || "默认")}</span></div>
    ${item.error ? `<p class="error-note">${escapeHtml(item.error)}</p>` : ""}
    ${retryable || revisable ? `<section class="subtask-action-panel restart-editor">
      <header class="subtask-action-heading"><div><strong>${restartTitle}</strong><span>新会话，可同时更换执行器和模型</span></div></header>
      <p class="action-target">目标：<strong>${escapeHtml(item.plan_key)} · ${escapeHtml(item.title)}</strong></p>
      <label class="subtask-field">本轮新增要求${retryable ? ' <span>可选</span>' : ""}<textarea data-subtask-requirement="${escapeHtml(item.id)}" rows="4" placeholder="写清本轮要补充、修正或重点检查的内容${retryable ? "；不填则按原要求重试" : ""}"></textarea></label>
      <div class="dispatch-fields">
        <label>执行器<select data-subtask-executor="${escapeHtml(item.id)}">${executorOptions(item.executor)}</select></label>
        <label>模型${modelPicker("subtask", item.id, item.executor, selectedModel)}</label>
      </div>
      <div class="subtask-action-footer"><span>只有点击右侧确认按钮才会启动。</span><button class="primary-button" type="button" data-action="${restartAction}" data-subtask-id="${escapeHtml(item.id)}">${restartButton}</button></div>
    </section>` : ""}
    ${resumable ? `<details class="resume-editor" data-task-panel="resume-${escapeHtml(item.id)}" ${taskPanelOpen(task.id, `resume-${item.id}`)}>
      <summary>继续原会话</summary>
      <div class="resume-editor-body">
        <p class="action-target">发送到：<strong>${escapeHtml(item.plan_key)} · ${escapeHtml(item.title)}</strong></p>
        <p class="resume-note">继续使用 ${escapeHtml(snap.name || item.executor || "原执行器")} · ${escapeHtml(selectedModel || "默认模型")}，这里不能切换执行器或模型。</p>
        <label class="subtask-field">追加说明<textarea data-subtask-message="${escapeHtml(item.id)}" rows="4" placeholder="只发送给这个子任务的原 Agent 会话"></textarea></label>
        <div class="subtask-action-footer"><span>只有点击右侧按钮才会发送并继续运行。</span><button class="quiet-button" type="button" data-action="message" data-subtask-id="${escapeHtml(item.id)}">发送并继续</button></div>
      </div>
    </details>` : ""}
    </div>
  </details>`;
}

function renderTask(task) {
  main.dataset.taskViewId = task.id;
  const logEvents = visibleLogEvents(task.events);
  const verifier = task.role_settings?.verifier || {};
  const allSubtasksSucceeded = task.subtasks.length && task.subtasks.every((item) => item.status === "succeeded");
  const verifierEditable = task.status === "awaiting_approval" || (["needs_attention", "failed"].includes(task.status) && allSubtasksSucceeded);
  const verification = task.plan?.verification || {};
  const verificationResults = verification.results || [];
  const manualVerificationAllowed = task.status === "needs_attention"
    && verificationResults.some((item) => item.status === "unclear")
    && !verificationResults.some((item) => item.status === "failed")
    && allSubtasksSucceeded;
  const ready = task.subtasks.length && task.subtasks.every((item) => item.executor) && verifier.executor;
  const actionButtons = [];
  if (task.status === "awaiting_approval") {
    actionButtons.push('<button class="quiet-button" data-action="replan">调整计划</button>');
    actionButtons.push(`<button class="primary-button" data-action="start" ${ready ? "" : "disabled"}>确认并执行</button>`);
  }
  if (["planning", "running", "verifying"].includes(task.status)) actionButtons.push('<button class="danger-button" data-action="cancel">停止任务</button>');
  if (["completed", "needs_attention", "failed"].includes(task.status) && allSubtasksSucceeded) actionButtons.push('<button class="quiet-button" data-action="verify">重新验收</button>');
  if (["completed", "needs_attention"].includes(task.status) && task.plan?.verification?.passed && !task.delivered_commit) actionButtons.push('<button class="primary-button" data-action="deliver">应用结果</button>');
  if (task.status === "completed" && task.delivered_commit) actionButtons.push('<button class="primary-button" data-action="continue-task">继续修改</button>');
  if (["completed", "needs_attention", "failed", "cancelled"].includes(task.status)) actionButtons.push('<button class="quiet-button" data-action="rerun">重新运行</button>');
  if (!task.cleaned_at && task.integration_branch && ["completed", "needs_attention", "failed", "cancelled"].includes(task.status)) actionButtons.push('<button class="quiet-button" data-action="cleanup-git">清理临时数据</button>');
  if (!["planning", "running", "verifying"].includes(task.status)) actionButtons.push('<button class="danger-button" data-action="delete-task">删除任务</button>');
  main.innerHTML = `
    <section class="page-heading"><div><h2 class="page-title">${escapeHtml(task.title)}</h2><p class="page-subtitle">${escapeHtml(task.plan?.summary || task.goal)}</p></div><div class="page-actions">${actionButtons.join("")}</div></section>
    <section class="metric-grid">
      <div class="metric"><span class="metric-label">状态</span><strong>${escapeHtml(statusLabels[task.status] || task.status)}</strong></div>
      <div class="metric"><span class="metric-label">进度</span><strong>${task.progress}%</strong></div>
      <div class="metric"><span class="metric-label">仓库</span><strong>${escapeHtml(task.repository_alias)}</strong></div>
      <div class="metric"><span class="metric-label">并发</span><strong>${task.max_concurrency}</strong></div>
    </section>
    ${task.error ? `<div class="alert error">${escapeHtml(task.error)}</div>` : ""}
    ${task.parent_task_id ? `<div class="alert">继续自任务 <code>${escapeHtml(task.parent_task_id.slice(0, 8).toUpperCase())}</code></div>` : ""}
    ${assignment(task, "verifier")}
    ${runHistory(task.runs || [])}
    <section class="panel task-operation-note"><p>规划阶段可以直接编辑子任务。重试、修改和追加都只会在点击对应确认按钮后运行。</p></section>
    <section class="panel">
      <header class="panel-header"><h3>子任务</h3></header>
      <div class="subtask-list">${task.subtasks.map((item) => subtaskCard(task, item)).join("") || '<div class="empty-panel">正在生成计划…</div>'}</div>
    </section>
    ${verifierEditable ? `<section class="panel verifier-panel">
      <header class="panel-header"><h3>${task.status === "awaiting_approval" ? "最终验收" : "重新验收设置"}</h3></header>
      <div class="dispatch-editor">
        <label>执行器<select id="verifier-executor"><option value="">请选择</option>${executorOptions(verifier.executor)}</select></label>
        <label>模型${modelPicker("verifier", task.id, verifier.executor, verifier.model || "")}</label>
        <button class="quiet-button" data-action="save-verifier">保存验收设置</button>
      </div>
    </section>` : ""}
    ${verificationResults.length ? `<section class="panel verification-panel">
      <header class="panel-header"><h3>验收结果</h3><span>${verification.passed ? "通过" : "需要确认"}</span></header>
      <div class="settings-body"><p>${escapeHtml(verification.summary || "")}</p></div>
      <div class="verification-list">${verificationResults.map((item) => `<div><strong>${escapeHtml(item.id)}</strong><span class="verification-status ${escapeHtml(item.status)}">${item.status === "passed" ? "通过" : item.status === "failed" ? "失败" : "不确定"}</span><p>${escapeHtml(item.evidence)}</p></div>`).join("")}</div>
      ${manualVerificationAllowed ? `<div class="manual-verification"><p>自动验收没有发现明确失败。请在目标目录用真实浏览器完成不确定项，再填写实际验证证据。</p><button class="primary-button" data-action="accept-verification">人工确认并应用</button></div>` : ""}
    </section>` : ""}
    <section class="content-grid two-up">
      <article class="panel"><header class="panel-header"><h3>验收标准</h3></header><div class="acceptance-list">${(task.plan?.acceptance || []).map((item) => `<div><strong>${escapeHtml(item.id)}</strong><span>${escapeHtml(item.criterion)}</span></div>`).join("") || "尚未生成"}</div></article>
      <article class="panel"><header class="panel-header"><h3>Git 结果</h3></header><div class="settings-body"><p>工作目录：<code>${escapeHtml(task.working_subdir || "仓库根目录")}</code></p><p>集成分支：<code>${escapeHtml(task.integration_branch || "确认后创建")}</code></p><p>目标分支：<code>${escapeHtml(task.base_branch || "确认后确定")}</code></p><p>交付：${task.delivered_commit ? `已应用 <code>${escapeHtml(task.delivered_commit.slice(0, 12))}</code>` : "尚未应用"}</p><p>临时数据：${task.cleaned_at ? "已清理" : task.integration_branch ? "保留中" : "尚未创建"}</p></div></article>
    </section>
    <details class="panel" data-task-panel="logs" ${taskPanelOpen(task.id, "logs")}><summary>运行日志（${logEvents.length}）</summary>${task.events.length ? '<div class="details-actions"><button class="quiet-button" type="button" data-action="clear-logs">清空日志</button></div>' : ""}<div class="event-list" data-task-scroll="logs" tabindex="0">${logEvents.slice().reverse().map((event) => { const log = logPresentation(event); return `<div><time datetime="${escapeHtml(log.sourceTime)}" title="原始时间：${escapeHtml(log.sourceTime)}">${escapeHtml(log.time)}</time><strong>${escapeHtml(event.kind)}</strong><p>${escapeHtml(log.message)}</p></div>`; }).join("") || '<div class="empty-panel">暂无日志</div>'}</div></details>
    <details class="panel" data-task-panel="documents" ${taskPanelOpen(task.id, "documents")}><summary>任务文档</summary>${Object.entries(task.documents || {}).map(([name, content]) => `<h4>${name}</h4><pre class="document-view" data-task-scroll="document-${escapeHtml(name)}">${escapeHtml(content)}</pre>`).join("")}</details>`;
  restoreTaskView(task.id);
}

const settingsSections = Object.freeze([
  { id: "agents", label: "Agent 配置", description: "规划、执行与验收默认值" },
  { id: "credentials", label: "模型凭证", description: "CLI 登录与个人 API Key" },
  { id: "skills", label: "Skill 配置", description: "个人任务分发规则" },
  { id: "repositories", label: "仓库管理", description: "创建或克隆个人仓库" },
]);

function renderAgentSettings() {
  const preferences = state.settings?.preferences || {};
  const roleField = (role, label) => {
    const preference = preferences[role] || {};
    return `<div class="field"><label>${label} Agent</label><select name="${role}Executor" data-settings-executor="${role}">${executorOptions(preference.executor)}</select></div>
      <div class="field"><label>${label}模型</label>${settingsModelPicker(role, preference)}</div>`;
  };
  return `<header class="settings-section-heading"><div><h2>Agent 配置</h2><p>设置规划、执行与验收默认使用的 Agent 和模型。</p></div></header>
    <article class="panel"><header class="panel-header"><h3>Agent 与模型</h3></header>
      <form class="settings-body settings-role-form" id="agent-settings-form">
        ${roleField("planner", "规划")}
        ${roleField("worker", "执行")}
        ${roleField("verifier", "验收")}
        <div class="form-actions full"><button class="primary-button" type="submit">保存 Agent 设置</button></div>
      </form>
    </article>
    <article class="panel"><header class="panel-header"><h3>执行器状态</h3></header>
      <div class="executor-list">${(state.settings?.executors || []).map((item) => `<div class="executor-row"><div class="executor-copy"><strong>${escapeHtml(item.name)}</strong><p>${escapeHtml(item.kind)}</p>${item.error ? `<span class="repository-error">${escapeHtml(item.error)}</span>` : ""}</div><span class="availability-badge ${item.available ? "" : "offline"}">${item.available ? "可用" : "不可用"}</span></div>`).join("")}</div>
      <div class="settings-limits"><span>服务器全局并发 <strong>${state.settings?.globalConcurrency || 4}</strong></span><span>我的并发 <strong>${state.settings?.tenantRuns?.limit || 1}</strong></span></div>
    </article>`;
}

function renderCredentialSettings() {
  const credentials = state.me?.credentials || {};
  const credentialForm = (provider, label) => {
    const status = credentials[provider] || {};
    return `<form class="settings-body credential-form" data-provider="${provider}">
      <div class="field"><label>${label} 认证</label><select name="mode"><option value="cli" ${status.mode !== "api_key" ? "selected" : ""}>CLI 登录</option><option value="api_key" ${status.mode === "api_key" ? "selected" : ""}>个人 API Key</option></select></div>
      <div class="field"><label>${label} API Key <span class="field-hint">可选</span></label><input name="key" type="password" autocomplete="off" placeholder="${escapeHtml(status.preview || "尚未配置")}"></div>
      <div class="form-actions"><button class="primary-button" type="submit">保存认证设置</button>${status.configured ? `<button class="quiet-button" type="button" data-action="delete-credential" data-provider="${provider}">删除 Key</button>` : ""}</div>
    </form>`;
  };
  return `<header class="settings-section-heading"><div><h2>模型凭证</h2><p>选择每个 Provider 使用独立 CLI 登录或个人 API Key。</p></div><span>${escapeHtml(state.me?.person || "")}</span></header>
    <div class="settings-notice">未填写 API Key 时，默认使用你空间中的 Codex CLI 登录。</div>
    <article class="panel credential-panel"><header class="panel-header"><h3>OpenAI / Codex</h3></header>${credentialForm("openai", "OpenAI / Codex")}</article>
    <article class="panel credential-panel"><header class="panel-header"><h3>Anthropic / Claude</h3></header>${credentialForm("anthropic", "Anthropic / Claude")}</article>`;
}

function renderSkillSettings(skill) {
  return `<header class="settings-section-heading"><div><h2>Skill 配置</h2><p>定制当前用户的任务拆分和分发规则。</p></div><span>${skill.customized ? "已定制" : "内置模板"}</span></header>
    <article class="panel"><form class="settings-body" id="skill-settings-form">
      <div class="field full"><label>orchestration/SKILL.md</label><textarea name="content" rows="22" maxlength="65536" required>${escapeHtml(skill.content || "")}</textarea></div>
      <div class="form-actions"><button class="primary-button" type="submit">保存 Skill</button><button class="quiet-button" type="button" data-action="reset-skill">恢复默认</button></div>
    </form></article>`;
}

function renderRepositorySettings() {
  const form = `<form class="settings-body repository-form managed-repository-form" id="repository-form">
        <div class="field"><label>仓库名称</label><input name="alias" required maxlength="40" pattern="[a-zA-Z0-9._-]+" placeholder="my-repo"></div>
        <div class="field"><label>Git HTTPS URL <span class="field-hint">可选</span></label><input name="cloneUrl" type="url" inputmode="url" placeholder="https://github.com/org/repo.git"></div>
        <button class="primary-button" type="submit">创建仓库</button>
      </form>`;
  return `<header class="settings-section-heading"><div><h2>仓库管理</h2><p>创建空仓库或从 HTTPS Git URL 克隆，存储位置由 FastLab 自动管理。</p></div><span>${state.repositories.length} 个</span></header>
    <article class="panel">
      ${form}
      <div class="repository-list">${state.repositories.map((item) => `<article class="repository-row"><div><strong>${escapeHtml(item.alias)}</strong>${item.is_default ? '<span class="default-badge">默认</span>' : ""}<p>${item.source === "clone" ? "从远程仓库克隆" : "FastLab 创建的空仓库"}</p>${item.error ? `<span class="repository-error">${escapeHtml(item.error)}</span>` : ""}</div><div class="repository-actions">${item.initializable ? `<button class="quiet-button" data-action="initialize-repository" data-repository-id="${item.id}">创建初始提交</button>` : ""}${item.is_default ? "" : `<button class="quiet-button" data-action="default-repository" data-repository-id="${item.id}">设为默认</button>`}${item.deletable ? `<button class="danger-button" data-action="delete-managed-repository" data-repository-id="${item.id}">永久删除仓库</button>` : ""}</div></article>`).join("") || '<div class="empty-panel">暂无仓库</div>'}</div>
    </article>`;
}

async function renderSettings() {
  leaveTaskView();
  const allowedSections = new Set(settingsSections.map((item) => item.id));
  if (!allowedSections.has(state.settingsSection)) state.settingsSection = "agents";
  let content;
  if (state.settingsSection === "credentials") content = renderCredentialSettings();
  else if (state.settingsSection === "skills") {
    if (!state.skill) state.skill = await api("/api/settings/skill");
    content = renderSkillSettings(state.skill || state.settings?.orchestrationSkill || {});
  } else if (state.settingsSection === "repositories") content = renderRepositorySettings();
  else content = renderAgentSettings();
  main.innerHTML = `<section class="settings-page">
    <header class="settings-topbar">
      <button class="settings-back-button" type="button" data-action="settings-back" aria-label="返回之前的页面"><span aria-hidden="true">←</span> 返回</button>
      <div><span class="settings-eyebrow">FastLab</span><h1>设置</h1></div>
    </header>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="设置分类">${settingsSections.map((item) => `<button class="settings-nav-button ${item.id === state.settingsSection ? "active" : ""}" type="button" data-action="settings-section" data-settings-section="${item.id}" ${item.id === state.settingsSection ? 'aria-current="page"' : ""}><strong>${item.label}</strong><span>${item.description}</span></button>`).join("")}</nav>
      <section class="settings-content" aria-live="polite">${content}</section>
    </div>
  </section>`;
}

export async function render() {
  const version = ++state.renderVersion;
  rememberTaskView();
  state.mainDirty = false;
  renderSidebar();
  if (state.page === "agents") { leaveTaskView(); main.innerHTML = libraryView(); return; }
  if (state.page === "new") return renderCreate();
  if (state.page === "settings") return renderSettings();
  if (state.selectedTaskId) {
    const taskId = state.selectedTaskId;
    const task = await api(`/api/tasks/${taskId}`);
    if (version !== state.renderVersion || taskId !== state.selectedTaskId) return;
    return renderTask(task);
  }
  renderHome();
}

export function canAutoRenderMain() {
  const active = document.activeElement;
  const editing = active && main.contains(active) && active.matches("input, textarea, select");
  const selection = window.getSelection();
  const selecting = selection && !selection.isCollapsed && main.contains(selection.anchorNode);
  return !state.mainDirty && !editing && !selecting;
}
