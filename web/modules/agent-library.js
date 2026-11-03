import { state } from "./state.js";
import { escapeHtml as e } from "./presentation.js";

export function agentOptions(role, selected = "") {
  return `<option value="">使用默认定义</option>${(state.agents || []).filter(a => a.roles.includes(role)).map(a => `<option value="${e(a.id)}" ${a.id === selected ? "selected" : ""}>${e(a.name)} · v${a.version}</option>`).join("")}`;
}

export function agentForm(agent = {}) {
  const executors = state.settings?.executors || [];
  const executor = agent.executor || "codex";
  const models = executors.find(x => x.id === executor)?.models || [];
  const modelIds = models.map(x => typeof x === "string" ? x : x.model || x.slug || x.id);
  if (agent.model && !modelIds.includes(agent.model)) modelIds.push(agent.model);
  return `<form id="agent-library-form" class="panel task-form" data-id="${e(agent.id || "")}" data-version="${agent.version || 0}">
    <div class="field"><label for="agent-name">名称</label><input id="agent-name" name="name" maxlength="80" required value="${e(agent.name || "")}"></div>
    <div class="field"><label for="agent-role">适用角色</label><select id="agent-role" name="roles" multiple size="3">${[["planner", "规划"], ["worker", "执行"], ["verifier", "验收"]].map(([r,n]) => `<option value="${r}" ${(agent.roles || ["worker"]).includes(r) ? "selected" : ""}>${n}</option>`).join("")}</select></div>
    <div class="field full"><label for="agent-description">职责</label><textarea id="agent-description" name="description" maxlength="2000" rows="2">${e(agent.description || "")}</textarea></div>
    <div class="field full"><label for="agent-instructions">工作指令</label><textarea id="agent-instructions" name="instructions" maxlength="20000" rows="5">${e(agent.instructions || "")}</textarea></div>
    <div class="field full"><label for="agent-skills">Skills 内容</label><textarea id="agent-skills" name="skills" maxlength="40000" rows="5">${e(agent.skills || "")}</textarea></div>
    <div class="field"><label for="agent-executor">执行器</label><select id="agent-executor" name="executor" data-settings-executor="library">${executors.map(x => `<option value="${e(x.id)}" ${x.id === executor ? "selected" : ""}>${e(x.name)}</option>`).join("")}</select></div>
    <div class="field"><label for="agent-model">模型</label><select id="agent-model" name="model" data-settings-model="library"><option value="">CLI 默认模型</option>${modelIds.map(m => `<option value="${e(m)}" ${m === agent.model ? "selected" : ""}>${e(m)}</option>`).join("")}</select></div>
    <div class="field"><label for="agent-effort">推理等级</label><select id="agent-effort" name="reasoningEffort">${["low", "medium", "high", "xhigh", "max", "ultra"].map(x => `<option ${(agent.reasoningEffort || "high") === x ? "selected" : ""}>${x}</option>`).join("")}</select></div>
    <div class="form-actions full"><button class="primary-button" type="submit">${state.agentAssignment ? "保存任务内定义" : "保存定义"}</button><button class="quiet-button" type="button" data-action="agent-draft-cancel">取消</button></div>
  </form>`;
}

export function libraryView() {
  return `<section class="page-heading"><div><h2 class="page-title">Agent 库</h2><p>保存可复用的能力定义，每个任务使用独立执行会话。</p></div><button class="primary-button" data-action="agent-new">创建 Agent</button></section>
    ${state.agentDraft ? agentForm(state.agentDraft) : `<div class="agent-library-grid">${(state.agents || []).map(a => `<article class="panel settings-body"><h3>${e(a.name)} <small>v${a.version}</small></h3><p>${e(a.description || "尚未填写职责")}</p><p>${e(a.executor)} · ${e(a.model || "默认模型")} · ${a.roles.map(r => ({planner:"规划",worker:"执行",verifier:"验收"}[r])).join(" / ")}</p><div class="page-actions"><button class="quiet-button" data-action="agent-edit" data-agent-id="${e(a.id)}">编辑</button><button class="quiet-button" data-action="agent-copy" data-agent-id="${e(a.id)}">复制</button><button class="danger-button" data-action="agent-archive" data-agent-id="${e(a.id)}">归档</button></div></article>`).join("") || '<p>还没有 Agent，创建一个能力定义开始。</p>'}</div>`}`;
}

export function assignment(task, role, subtask = null) {
  const definition = subtask?.executor_snapshot?.agent || task.role_settings?.[role]?.agent;
  const id = subtask?.id || "verifier";
  return `<section class="agent-assignment"><p>Agent：${e(definition?.name || "默认执行配置")}${definition?.version ? ` · v${definition.version}` : ""}</p>
    ${task.status === "awaiting_approval" ? `<label>分配 Agent <select data-agent-choice="${e(id)}">${agentOptions(role, definition?.id).replace("使用默认定义", "选择已有定义")}</select></label><button class="quiet-button" data-action="agent-assign" data-role="${role}" data-subtask-id="${e(subtask?.id || "")}">应用选择</button><button class="quiet-button" data-action="agent-customize" data-role="${role}" data-subtask-id="${e(subtask?.id || "")}">编辑任务内定义</button>` : ""}
    ${definition ? `<button class="quiet-button" data-action="agent-save-snapshot" data-role="${role}" data-subtask-id="${e(subtask?.id || "")}">另存到 Agent 库</button>` : ""}</section>`;
}

export function runHistory(runs = []) {
  const labels = {queued:"排队",running:"运行中",succeeded:"调用成功",failed:"失败",cancelled:"已取消",timed_out:"超时",interrupted:"服务中断"};
  return `<details class="panel"><summary>运行历史（${runs.length}）</summary><div class="settings-body">${runs.map(r => `<article class="run-history-row"><strong>${e(r.snapshot?.agent?.name || r.role)} · ${e(labels[r.status] || r.status)}</strong><p>${e(r.executor)} · ${e(r.started_at)} → ${e(r.finished_at || "进行中")}</p><p>实例 ${e(r.instance_id)} · 会话 ${e(r.session_id || "尚未创建")}</p>${r.error ? `<p class="error-note">${e(r.error)}</p>` : ""}</article>`).join("") || "尚无运行记录"}</div></details>`;
}
