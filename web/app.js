const state = {
  tasks: [], repositories: [], settings: null, health: null,
  page: "home", selectedTaskId: null, timer: null, mainDirty: false,
  renderVersion: 0, taskViews: {},
};

const main = document.querySelector("#main-content");
const taskList = document.querySelector("#task-list");
const taskCount = document.querySelector("#task-count");
const workspaceLabel = document.querySelector("#workspace-label");

const statusLabels = {
  planning: "正在规划", awaiting_approval: "等待确认", running: "执行中",
  verifying: "正在验收", completed: "已完成", needs_attention: "需要处理",
  failed: "失败", cancelled: "已取消", pending: "等待", succeeded: "已完成",
  blocked: "被阻塞",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function formatLocalTime(value) {
  const source = String(value || "").trim();
  if (!source) return "";
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(source) ? source : `${source}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return source;
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function logPresentation(event) {
  const message = String(event?.message || "");
  const embedded = message.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+([\s\S]*)$/);
  const sourceTime = embedded ? embedded[1] : event?.created_at;
  return {
    sourceTime,
    time: formatLocalTime(sourceTime),
    message: embedded ? embedded[2] : message,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（${response.status}）`);
    error.code = payload.code;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message, type = "info") {
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
  return (item?.models || []).map((value) => String(value.model || value.slug || value.id || "").trim()).filter(Boolean);
}

function modelPicker(kind, id, executor, selected = "") {
  const listId = `models-${kind}-${id}`;
  const attribute = kind === "verifier" ? 'id="verifier-model"' : `data-subtask-model="${escapeHtml(id)}"`;
  return `<input ${attribute} list="${escapeHtml(listId)}" value="${escapeHtml(selected)}" placeholder="默认模型">
    <datalist id="${escapeHtml(listId)}">${executorModels(executor).map((model) => `<option value="${escapeHtml(model)}"></option>`).join("")}</datalist>`;
}

function updateModelSuggestions(input, executor) {
  if (!input) return;
  input.value = "";
  const list = document.getElementById(input.getAttribute("list"));
  if (list) list.innerHTML = executorModels(executor).map((model) => `<option value="${escapeHtml(model)}"></option>`).join("");
}

function plannerLabel(planner = {}) {
  return planner.backend === "claude" ? "Claude CLI" : "Codex CLI";
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

function visibleLogEvents(events = []) {
  return events.filter((event) => {
    const kind = String(event.kind || "").trim();
    const message = String(event.message || "").trim();
    if (!message) return false;
    const leaf = kind.split(".").pop();
    return message !== kind && message !== leaf && !["user", "assistant"].includes(message);
  });
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

function leaveTaskView() {
  main.removeAttribute("data-task-view-id");
}

function renderSidebar() {
  taskCount.textContent = state.tasks.length;
  const current = state.repositories.find((item) => item.is_default);
  workspaceLabel.textContent = current ? `${current.alias}${current.available ? "" : " · 不可用"}` : "尚未登记";
  taskList.innerHTML = state.tasks.length ? state.tasks.map((task) => `
    <button class="task-nav-item ${task.id === state.selectedTaskId ? "active" : ""}" type="button" data-task-id="${task.id}">
      <span class="nav-status-dot ${task.status}"></span>
      <span class="task-nav-copy"><strong>${escapeHtml(task.title)}</strong><span>${escapeHtml(statusLabels[task.status] || task.status)}</span></span>
      <span class="nav-progress">${task.progress}%</span>
    </button>`).join("") : '<div class="task-list-empty">暂无任务</div>';
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
        ${ready ? "" : '<button class="quiet-button" type="button" data-action="settings">添加仓库</button>'}
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
    main.innerHTML = '<section class="empty-state"><h2>没有可用仓库</h2><button class="primary-button" data-action="settings">添加仓库</button></section>';
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
      <div class="field"><label>规划工具</label><input value="${plannerLabel(planner)}" readonly></div>
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

function renderSettings() {
  leaveTaskView();
  const planner = state.settings?.planner || {};
  const feishu = state.health?.feishu || {};
  main.innerHTML = `
    <section class="page-heading"><div><h2 class="page-title">设置</h2></div></section>
    <section class="content-grid two-up">
      <article class="panel"><header class="panel-header"><h3>规划</h3></header>
        <form class="settings-body" id="executor-settings-form">
          <div class="field"><label>规划工具</label><input value="${plannerLabel(planner)}" readonly></div>
          <p class="privacy-note full">在 <code>fastlab.env</code> 中修改，重启后生效。</p>
          <div class="field"><label>全局并发</label><input name="globalConcurrency" type="number" min="1" max="${state.settings?.maxGlobalConcurrency || 32}" value="${state.settings?.globalConcurrency || 4}"></div>
          <button class="primary-button" type="submit">保存并发设置</button>
        </form>
      </article>
      <article class="panel"><header class="panel-header"><h3>执行器</h3></header>
        <div class="executor-list">${(state.settings?.executors || []).map((item) => `<div class="executor-row"><div class="executor-copy"><strong>${escapeHtml(item.name)}</strong><p>${escapeHtml(item.kind)}</p>${item.error ? `<span class="repository-error">${escapeHtml(item.error)}</span>` : ""}</div><span class="availability-badge ${item.available ? "" : "offline"}">${item.available ? "可用" : "不可用"}</span></div>`).join("")}</div>
      </article>
      <article class="panel"><header class="panel-header"><h3>飞书</h3><span>${feishu.connected ? "已连接" : feishu.configured ? "连接异常" : "未配置"}</span></header>${feishu.error ? `<div class="settings-body"><p>${escapeHtml(feishu.error)}</p></div>` : ""}</article>
    </section>
    <section class="panel"><header class="panel-header"><h3>仓库</h3><span>${state.repositories.length} 个</span></header>
      <div class="repository-list">${state.repositories.map((item) => `<article class="repository-row"><div><strong>${escapeHtml(item.alias)}</strong>${item.is_default ? '<span class="default-badge">默认</span>' : ""}<p>${escapeHtml(item.path)}</p>${item.error ? `<span class="repository-error">${escapeHtml(item.error)}</span>` : ""}</div><div class="repository-actions">${item.initializable ? `<button class="quiet-button" data-action="initialize-repository" data-repository-id="${item.id}">创建初始提交</button>` : ""}${item.is_default ? "" : `<button class="quiet-button" data-action="default-repository" data-repository-id="${item.id}">设为默认</button>`}<button class="quiet-button" data-action="delete-repository" data-repository-id="${item.id}">移除</button></div></article>`).join("") || '<div class="empty-panel">暂无仓库</div>'}</div>
      <form class="settings-body repository-form" id="repository-form"><div class="field"><label>别名</label><input name="alias" required placeholder="my-repo"></div><div class="field"><label>本机路径</label><input name="path" required placeholder="/Users/me/Code/project"></div><button class="primary-button" type="submit">添加仓库</button></form>
    </section>`;
}

async function render() {
  const version = ++state.renderVersion;
  rememberTaskView();
  state.mainDirty = false;
  renderSidebar();
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

function canAutoRenderMain() {
  const active = document.activeElement;
  const editing = active && main.contains(active) && active.matches("input, textarea, select");
  const selection = window.getSelection();
  const selecting = selection && !selection.isCollapsed && main.contains(selection.anchorNode);
  return !state.mainDirty && !editing && !selecting;
}

async function refresh({ keepPage = true, renderMain = true, respectEditing = false } = {}) {
  const [tasks, repositories, settings, health] = await Promise.all([
    api("/api/tasks"), api("/api/repositories"), api("/api/settings/executors"),
    api("/api/health"),
  ]);
  state.tasks = tasks.tasks;
  state.repositories = repositories.repositories;
  state.settings = settings;
  state.health = health;
  if (!keepPage) { state.page = "home"; state.selectedTaskId = null; }
  if (renderMain && (!respectEditing || canAutoRenderMain())) await render();
  else renderSidebar();
}

function markDraft(event) {
  if (main.contains(event.target) && event.target.matches("input, textarea, select")) {
    state.mainDirty = true;
  }
}

document.addEventListener("input", markDraft);
document.addEventListener("change", markDraft);
document.addEventListener("change", (event) => {
  if (event.target.matches("[data-subtask-executor]")) {
    const id = event.target.dataset.subtaskExecutor;
    updateModelSuggestions(
      document.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`),
      event.target.value,
    );
  } else if (event.target.id === "verifier-executor") {
    updateModelSuggestions(document.querySelector("#verifier-model"), event.target.value);
  }
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  try {
    if (form.id === "task-form") {
      const task = await api("/api/tasks", { method: "POST", body: JSON.stringify({
        repositoryId: data.get("repositoryId"), workingSubdir: data.get("workingSubdir"),
        title: data.get("title"), goal: data.get("goal"), constraints: data.get("constraints"),
        maxConcurrency: Number(data.get("maxConcurrency")),
      }) });
      state.mainDirty = false;
      state.page = "task"; state.selectedTaskId = task.id; await refresh();
    } else if (form.id === "executor-settings-form") {
      await api("/api/settings/executors", { method: "PUT", body: JSON.stringify({
        globalConcurrency: Number(data.get("globalConcurrency")),
      }) });
      state.mainDirty = false;
      toast("设置已保存。", "success"); await refresh();
    } else if (form.id === "repository-form") {
      const repository = { alias: data.get("alias"), path: data.get("path") };
      let initialized = false;
      try {
        await api("/api/repositories", { method: "POST", body: JSON.stringify(repository) });
      } catch (error) {
        if (error.code !== "repository_initialization_required") throw error;
        const confirmed = window.confirm(`${error.message}\n\nFastLab 会把未被 .gitignore 忽略的现有文件加入首次提交。请先确认目录中没有不应提交的敏感文件。`);
        if (!confirmed) return;
        await api("/api/repositories", { method: "POST", body: JSON.stringify({ ...repository, initialize: true }) });
        initialized = true;
      }
      state.mainDirty = false;
      form.reset(); toast(initialized ? "仓库已初始化并添加。" : "仓库已添加。", "success"); await refresh();
    }
  } catch (error) { toast(error.message, "error"); }
});

document.addEventListener("click", async (event) => {
  const taskButton = event.target.closest("[data-task-id]");
  if (taskButton) {
    state.mainDirty = false;
    state.selectedTaskId = taskButton.dataset.taskId;
    state.page = "task";
    try { await render(); } catch (error) { toast(error.message, "error"); }
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "home") { state.mainDirty = false; state.page = "home"; state.selectedTaskId = null; return render(); }
    if (action === "new-task") { state.mainDirty = false; state.page = "new"; state.selectedTaskId = null; return render(); }
    if (action === "settings") { state.mainDirty = false; state.page = "settings"; state.selectedTaskId = null; return render(); }
    if (["start", "cancel", "verify"].includes(action)) {
      await api(`/api/tasks/${state.selectedTaskId}/${action}`, { method: "POST", body: "{}" });
      toast(action === "start" ? "任务已开始。" : action === "cancel" ? "已请求停止。" : "已开始重新验收。", "success"); return refresh();
    }
    if (action === "deliver") {
      const task = await api(`/api/tasks/${state.selectedTaskId}/deliver`, { method: "POST", body: "{}" });
      toast(
        task.status === "verifying"
          ? "已同步目标分支的新提交，重新验收通过后会自动应用。"
          : "结果已应用到目标目录。",
        "success",
      );
      return refresh();
    }
    if (action === "cleanup-git") {
      if (!window.confirm("清理这个任务的临时 Worktree 和分支？未交付的临时修改将无法恢复。")) return;
      await api(`/api/tasks/${state.selectedTaskId}/cleanup`, { method: "POST", body: "{}" });
      toast("临时 Worktree 和任务分支已清理。", "success");
      return refresh();
    }
    if (action === "rerun") {
      const task = await api(`/api/tasks/${state.selectedTaskId}/rerun`, { method: "POST", body: "{}" });
      state.selectedTaskId = task.id;
      state.page = "task";
      toast("已创建新的运行任务。", "success");
      return refresh();
    }
    if (action === "continue-task") {
      const message = window.prompt("继续修改什么？", "");
      if (!message || !message.trim()) return;
      const task = await api(`/api/tasks/${state.selectedTaskId}/continue`, {
        method: "POST", body: JSON.stringify({ message: message.trim() }),
      });
      state.selectedTaskId = task.id;
      state.page = "task";
      toast("已创建继续任务。", "success");
      return refresh();
    }
    if (action === "clear-logs") {
      if (!window.confirm("清空这个任务的全部日志？运行中的任务仍会继续产生新日志。")) return;
      await api(`/api/tasks/${state.selectedTaskId}/events`, { method: "DELETE" });
      toast("日志已清空。", "success");
      return refresh();
    }
    if (action === "delete-task") {
      if (!window.confirm("删除这个任务？日志、文档、临时 Worktree 和该任务的 Git 分支都会永久删除。未交付的修改将无法恢复。")) return;
      const taskId = state.selectedTaskId;
      await api(`/api/tasks/${taskId}`, { method: "DELETE" });
      delete state.taskViews[taskId];
      leaveTaskView();
      state.selectedTaskId = null;
      state.page = "home";
      toast("任务已删除。", "success");
      return refresh();
    }
    if (action === "replan") {
      const feedback = window.prompt("计划需要怎样调整？", "");
      if (feedback) await api(`/api/tasks/${state.selectedTaskId}/replan`, { method: "POST", body: JSON.stringify({ feedback }) });
      return refresh();
    }
    if (action === "save-subtask") {
      const id = button.dataset.subtaskId;
      await api(`/api/subtasks/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({
        title: document.querySelector(`[data-subtask-title="${CSS.escape(id)}"]`).value.trim(),
        instructions: document.querySelector(`[data-subtask-instructions="${CSS.escape(id)}"]`).value.trim(),
        executor: document.querySelector(`[data-subtask-executor="${CSS.escape(id)}"]`).value,
        model: document.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`).value.trim(),
      }) });
      state.mainDirty = false;
      toast("子任务已保存，不会自动开始执行。", "success"); return refresh();
    }
    if (action === "save-verifier") {
      const executor = document.querySelector("#verifier-executor").value;
      if (!executor) throw new Error("请选择验收执行器。");
      await api(`/api/tasks/${state.selectedTaskId}/verifier`, { method: "PUT", body: JSON.stringify({
        executor, model: document.querySelector("#verifier-model").value.trim(),
      }) });
      state.mainDirty = false;
      toast("验收设置已保存。", "success"); return refresh();
    }
    if (action === "retry") {
      const id = button.dataset.subtaskId;
      await api(`/api/subtasks/${encodeURIComponent(id)}/retry`, { method: "POST", body: JSON.stringify({
        message: document.querySelector(`[data-subtask-requirement="${CSS.escape(id)}"]`).value.trim(),
        executor: document.querySelector(`[data-subtask-executor="${CSS.escape(id)}"]`).value,
        model: document.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`).value.trim(),
      }) });
      state.mainDirty = false;
      toast("子任务已按本轮要求和新设置开始重试。", "success");
      return refresh();
    }
    if (action === "revise") {
      const id = button.dataset.subtaskId;
      const message = document.querySelector(`[data-subtask-requirement="${CSS.escape(id)}"]`).value.trim();
      if (!message) throw new Error("请填写本轮新增要求。");
      await api(`/api/subtasks/${encodeURIComponent(id)}/revise`, { method: "POST", body: JSON.stringify({
        message,
        executor: document.querySelector(`[data-subtask-executor="${CSS.escape(id)}"]`).value,
        model: document.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`).value.trim(),
      }) });
      toast("已使用新会话按新增要求重跑。", "success");
      state.mainDirty = false;
      return refresh();
    }
    if (action === "accept-verification") {
      const evidence = window.prompt("请写明你在目标目录完成了哪些人工检查，以及结果：", "");
      if (!evidence || !evidence.trim()) return;
      const task = await api(`/api/tasks/${state.selectedTaskId}/accept-verification`, {
        method: "POST", body: JSON.stringify({ evidence: evidence.trim() }),
      });
      toast(task.status === "completed" ? "人工验收已记录，结果已应用。" : "人工验收已记录，请处理交付提示。", "success");
      return refresh();
    }
    if (action === "message") {
      const id = button.dataset.subtaskId;
      const message = document.querySelector(`[data-subtask-message="${CSS.escape(id)}"]`).value.trim();
      if (!message) throw new Error("请填写要发送给原 Agent 的追加说明。");
      await api(`/api/subtasks/${encodeURIComponent(id)}/message`, {
        method: "POST", body: JSON.stringify({ message }),
      });
      state.mainDirty = false;
      toast("追加说明已发送给该子任务的原 Agent。", "success");
      return refresh();
    }
    if (action === "default-repository") {
      const item = state.repositories.find((value) => value.id === button.dataset.repositoryId);
      await api(`/api/repositories/${item.id}`, { method: "PUT", body: JSON.stringify({ alias: item.alias, path: item.path, isDefault: true }) });
    }
    if (action === "initialize-repository") {
      const confirmed = window.confirm("创建初始提交？\n\nFastLab 会把未被 .gitignore 忽略的现有文件加入首次提交。请先确认目录中没有不应提交的敏感文件。");
      if (!confirmed) return;
      await api(`/api/repositories/${button.dataset.repositoryId}/initialize`, { method: "POST", body: "{}" });
      toast("初始提交已创建。", "success");
    }
    if (action === "delete-repository") {
      if (!window.confirm("移除这个仓库登记？不会删除本机文件。")) return;
      await api(`/api/repositories/${button.dataset.repositoryId}`, { method: "DELETE" });
    }
    await refresh();
  } catch (error) { toast(error.message, "error"); }
});

async function start() {
  try {
    await refresh({ keepPage: false });
    state.timer = setInterval(() => refresh({ respectEditing: true }).catch(() => {}), 2500);
  } catch (error) {
    main.innerHTML = `<section class="empty-state"><h2>FastLab 暂时不可用</h2><p>${escapeHtml(error.message)}</p></section>`;
  }
}

start();
