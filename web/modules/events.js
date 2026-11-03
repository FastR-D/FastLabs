import { api } from "./api.js";
import { refresh } from "./application.js";
import {
  leaveTaskView,
  main,
  render,
  toast,
  updateModelSuggestions,
} from "./render.js";
import { state } from "./state.js";

export function markDraft(event) {
  if (main.contains(event.target) && event.target.matches("input, textarea, select")) {
    state.mainDirty = true;
  }
}

function canLeaveSettingsDraft() {
  return !["settings", "agents"].includes(state.page)
    || !state.mainDirty
    || window.confirm("当前设置尚未保存，确定离开吗？");
}

export function bindEvents(documentRef = document) {
  documentRef.addEventListener("input", markDraft);
  documentRef.addEventListener("change", markDraft);
  documentRef.addEventListener("change", (event) => {
    if (event.target.matches("[data-subtask-executor]")) {
      const id = event.target.dataset.subtaskExecutor;
      updateModelSuggestions(
        documentRef.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`),
        event.target.value,
      );
    } else if (event.target.id === "verifier-executor") {
      updateModelSuggestions(documentRef.querySelector("#verifier-model"), event.target.value);
    } else if (event.target.matches("[data-settings-executor]")) {
      const role = event.target.dataset.settingsExecutor;
      updateModelSuggestions(
        documentRef.querySelector(`[data-settings-model="${CSS.escape(role)}"]`),
        event.target.value,
      );
    }
  });

  documentRef.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const data = new FormData(form);
    try {
      if (form.id === "agent-library-form") {
        const body = Object.fromEntries(data.entries());
        body.roles = data.getAll("roles");
        body.version = Number(form.dataset.version);
        if (state.agentAssignment) {
          const { taskId, ...assignment } = state.agentAssignment;
          await api(`/api/tasks/${taskId}/agents`, { method: "POST", body: JSON.stringify({ ...assignment, temporaryAgent: body }) });
          state.selectedTaskId = taskId;
          state.page = "task";
          state.agentAssignment = null;
        } else {
          await api(`/api/agents${form.dataset.id ? `/${form.dataset.id}` : ""}`, {
            method: form.dataset.id ? "PUT" : "POST", body: JSON.stringify(body),
          });
        }
        state.mainDirty = false;
        state.agentDraft = null;
        await refresh();
      } else if (form.id === "task-form") {
        const task = await api("/api/tasks", { method: "POST", body: JSON.stringify({
          repositoryId: data.get("repositoryId"),
          workingSubdir: data.get("workingSubdir"),
          title: data.get("title"),
          goal: data.get("goal"),
          constraints: data.get("constraints"),
          maxConcurrency: Number(data.get("maxConcurrency")),
          agents: Object.fromEntries(["planner", "worker", "verifier"].filter(r => data.get(`${r}Agent`)).map(r => [r, data.get(`${r}Agent`)])),
        }) });
        state.mainDirty = false;
        state.page = "task";
        state.selectedTaskId = task.id;
        await refresh();
      } else if (form.id === "agent-settings-form") {
        const settings = {};
        for (const role of ["planner", "worker", "verifier"]) {
          settings[role] = {
            executor: data.get(`${role}Executor`),
            model: String(data.get(`${role}Model`) || "").trim(),
          };
        }
        await api("/api/settings/executors", {
          method: "PUT", body: JSON.stringify(settings),
        });
        state.mainDirty = false;
        toast("Agent 与模型设置已保存。", "success");
        await refresh();
      } else if (form.id === "skill-settings-form") {
        state.skill = await api("/api/settings/skill", {
          method: "PUT", body: JSON.stringify({ content: data.get("content") }),
        });
        state.mainDirty = false;
        toast("分发 Skill 已保存。", "success");
        await refresh();
      } else if (form.matches(".credential-form")) {
        const provider = form.dataset.provider;
        const key = String(data.get("key") || "").trim();
        const mode = key ? "api_key" : data.get("mode");
        if (key) {
          await api(`/api/settings/credentials/${provider}`, {
            method: "PUT", body: JSON.stringify({ key }),
          });
        }
        await api(`/api/settings/auth/${provider}`, {
          method: "PUT", body: JSON.stringify({ mode }),
        });
        state.mainDirty = false;
        form.reset();
        toast(mode === "cli" ? "已使用 CLI 登录。" : "API Key 认证设置已保存。", "success");
        await refresh();
      } else if (form.id === "repository-form") {
        const repository = {
          alias: data.get("alias"), cloneUrl: String(data.get("cloneUrl") || "").trim(),
        };
        await api("/api/repositories", { method: "POST", body: JSON.stringify(repository) });
        state.mainDirty = false;
        form.reset();
        toast(repository.cloneUrl ? "仓库已克隆。" : "空仓库已创建。", "success");
        await refresh();
      }
    } catch (error) {
      toast(error.message, "error");
    }
  });

  documentRef.addEventListener("click", async (event) => {
    const clickedMenu = event.target.closest(".task-nav-menu");
    const menuToggle = event.target.closest(".task-nav-menu > summary");
    for (const menu of documentRef.querySelectorAll?.(".task-nav-menu[open]") || []) {
      if (menu !== clickedMenu) menu.removeAttribute("open");
    }
    if (menuToggle) {
      const menu = menuToggle.parentElement;
      state.openTaskMenuId = menu.open ? null : menu.dataset.taskMenuId;
    } else if (!clickedMenu) {
      state.openTaskMenuId = null;
    }
    const taskButton = event.target.closest("[data-task-id]");
    if (taskButton) {
      if (!canLeaveSettingsDraft()) return;
      state.mainDirty = false;
      state.selectedTaskId = taskButton.dataset.taskId;
      state.page = "task";
      state.settingsOrigin = null;
      try {
        await render();
      } catch (error) {
        toast(error.message, "error");
      }
      return;
    }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const actionMenu = button.closest?.(".task-nav-menu");
    if (actionMenu) {
      actionMenu.removeAttribute("open");
      state.openTaskMenuId = null;
    }
    const action = button.dataset.action;
    try {
      if (["agent-library", "agent-new", "agent-edit", "agent-copy"].includes(action)) {
        if (!canLeaveSettingsDraft()) return;
        state.mainDirty = false;
        state.page = "agents";
        state.selectedTaskId = null;
        state.agentDraft = null;
        state.agentAssignment = null;
        if (action === "agent-new") state.agentDraft = {};
        if (["agent-edit", "agent-copy"].includes(action)) {
          state.agentDraft = await api(`/api/agents/${button.dataset.agentId}`);
          if (action === "agent-copy") {
            state.agentDraft = { ...state.agentDraft, id: "", version: 0, name: `${state.agentDraft.name} 副本` };
          }
        }
        return render();
      }
      if (action === "agent-archive") {
        if (!window.confirm("归档此 Agent？已有任务会保留当时的定义。")) return;
        await api(`/api/agents/${button.dataset.agentId}`, { method: "DELETE" });
        return refresh();
      }
      if (action === "agent-assign") {
        const id = button.dataset.subtaskId || "verifier";
        const agentId = documentRef.querySelector(`[data-agent-choice="${CSS.escape(id)}"]`).value;
        if (!agentId) throw new Error("请选择一个具体的 Agent 定义。");
        await api(`/api/tasks/${state.selectedTaskId}/agents`, { method: "POST", body: JSON.stringify({
          agentId, role: button.dataset.role, subtaskId: button.dataset.subtaskId || null,
        }) });
        state.mainDirty = false;
        return refresh();
      }
      if (action === "agent-draft-cancel") {
        if (!canLeaveSettingsDraft()) return;
        if (state.agentAssignment) {
          state.selectedTaskId = state.agentAssignment.taskId;
          state.page = "task";
        }
        state.agentDraft = null;
        state.agentAssignment = null;
        return render();
      }
      if (["agent-save-snapshot", "agent-customize"].includes(action)) {
        const task = await api(`/api/tasks/${state.selectedTaskId}`);
        const definition = button.dataset.subtaskId
          ? task.subtasks.find(s => s.id === button.dataset.subtaskId)?.executor_snapshot?.agent
          : task.role_settings?.[button.dataset.role]?.agent;
        state.agentAssignment = action === "agent-customize" ? {
          taskId: task.id, role: button.dataset.role, subtaskId: button.dataset.subtaskId || null,
        } : null;
        state.agentDraft = { name: "任务内 Agent", executor: "codex", roles: [button.dataset.role], ...definition, id: "", version: 0 };
        state.page = "agents";
        state.selectedTaskId = null;
        return render();
      }
      if (action === "home") {
        if (!canLeaveSettingsDraft()) return;
        state.mainDirty = false;
        state.page = "home";
        state.selectedTaskId = null;
        state.settingsOrigin = null;
        return render();
      }
      if (action === "new-task") {
        if (!canLeaveSettingsDraft()) return;
        state.mainDirty = false;
        state.page = "new";
        state.selectedTaskId = null;
        state.settingsOrigin = null;
        return render();
      }
      if (action === "settings") {
        if (!canLeaveSettingsDraft()) return;
        if (state.page !== "settings") {
          state.settingsOrigin = {
            page: state.page,
            selectedTaskId: state.selectedTaskId,
          };
        }
        state.mainDirty = false;
        if (button.dataset.settingsSection) {
          state.settingsSection = button.dataset.settingsSection;
        }
        state.page = "settings";
        state.selectedTaskId = null;
        return render();
      }
      if (action === "settings-back") {
        if (!canLeaveSettingsDraft()) return;
        const origin = state.settingsOrigin || { page: "home", selectedTaskId: null };
        state.mainDirty = false;
        state.page = origin.page || "home";
        state.selectedTaskId = origin.selectedTaskId || null;
        state.settingsOrigin = null;
        return render();
      }
      if (action === "settings-section") {
        const section = button.dataset.settingsSection;
        if (!section || section === state.settingsSection) return;
        if (!canLeaveSettingsDraft()) return;
        state.mainDirty = false;
        state.settingsSection = section;
        return render();
      }
      if (action === "delete-credential") {
        if (!window.confirm("删除这个 API Key？认证方式将切回对应 CLI。")) return;
        await api(`/api/settings/credentials/${button.dataset.provider}`, { method: "DELETE" });
        toast("凭证已删除，已切回 CLI 登录。", "success");
        return refresh();
      }
      if (action === "reset-skill") {
        if (!window.confirm("恢复内置分发 Skill？当前定制内容会被替换。")) return;
        state.skill = await api("/api/settings/skill", { method: "DELETE" });
        state.mainDirty = false;
        toast("已恢复内置分发 Skill。", "success");
        return refresh();
      }
      if (["start", "cancel", "verify"].includes(action)) {
        await api(`/api/tasks/${state.selectedTaskId}/${action}`, { method: "POST", body: "{}" });
        toast(action === "start" ? "任务已开始。" : action === "cancel" ? "已请求停止。" : "已开始重新验收。", "success");
        return refresh();
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
          method: "POST",
          body: JSON.stringify({ message: message.trim() }),
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
      if (action === "rename-task") {
        const taskId = button.dataset.taskActionId || state.selectedTaskId;
        if (!taskId) return;
        const task = state.tasks.find((item) => item.id === taskId);
        const title = window.prompt("重命名任务", task?.title || "");
        if (title === null) return;
        const normalizedTitle = title.trim();
        if (!normalizedTitle) throw new Error("任务名称不能为空。");
        await api(`/api/tasks/${taskId}`, {
          method: "PUT",
          body: JSON.stringify({ title: normalizedTitle }),
        });
        toast("任务已重命名。", "success");
        return refresh();
      }
      if (action === "delete-task") {
        if (!window.confirm("删除这个任务？日志、文档、临时 Worktree 和该任务的 Git 分支都会永久删除。未交付的修改将无法恢复。")) return;
        const taskId = button.dataset.taskActionId || button.dataset.deleteTaskId || state.selectedTaskId;
        if (!taskId) return;
        await api(`/api/tasks/${taskId}`, { method: "DELETE" });
        delete state.taskViews[taskId];
        if (taskId === state.selectedTaskId) {
          leaveTaskView();
          state.selectedTaskId = null;
          state.page = "home";
        }
        toast("任务已删除。", "success");
        return refresh();
      }
      if (action === "replan") {
        const feedback = window.prompt("计划需要怎样调整？", "");
        if (feedback) {
          await api(`/api/tasks/${state.selectedTaskId}/replan`, {
            method: "POST",
            body: JSON.stringify({ feedback }),
          });
        }
        return refresh();
      }
      if (action === "save-subtask") {
        const id = button.dataset.subtaskId;
        await api(`/api/subtasks/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({
          title: documentRef.querySelector(`[data-subtask-title="${CSS.escape(id)}"]`).value.trim(),
          instructions: documentRef.querySelector(`[data-subtask-instructions="${CSS.escape(id)}"]`).value.trim(),
          executor: documentRef.querySelector(`[data-subtask-executor="${CSS.escape(id)}"]`).value,
          model: documentRef.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`).value.trim(),
        }) });
        state.mainDirty = false;
        toast("子任务已保存，不会自动开始执行。", "success");
        return refresh();
      }
      if (action === "save-verifier") {
        const executor = documentRef.querySelector("#verifier-executor").value;
        if (!executor) throw new Error("请选择验收执行器。");
        await api(`/api/tasks/${state.selectedTaskId}/verifier`, { method: "PUT", body: JSON.stringify({
          executor,
          model: documentRef.querySelector("#verifier-model").value.trim(),
        }) });
        state.mainDirty = false;
        toast("验收设置已保存。", "success");
        return refresh();
      }
      if (action === "retry") {
        const id = button.dataset.subtaskId;
        await api(`/api/subtasks/${encodeURIComponent(id)}/retry`, { method: "POST", body: JSON.stringify({
          message: documentRef.querySelector(`[data-subtask-requirement="${CSS.escape(id)}"]`).value.trim(),
          executor: documentRef.querySelector(`[data-subtask-executor="${CSS.escape(id)}"]`).value,
          model: documentRef.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`).value.trim(),
        }) });
        state.mainDirty = false;
        toast("子任务已按本轮要求和新设置开始重试。", "success");
        return refresh();
      }
      if (action === "revise") {
        const id = button.dataset.subtaskId;
        const message = documentRef.querySelector(`[data-subtask-requirement="${CSS.escape(id)}"]`).value.trim();
        if (!message) throw new Error("请填写本轮新增要求。");
        await api(`/api/subtasks/${encodeURIComponent(id)}/revise`, { method: "POST", body: JSON.stringify({
          message,
          executor: documentRef.querySelector(`[data-subtask-executor="${CSS.escape(id)}"]`).value,
          model: documentRef.querySelector(`[data-subtask-model="${CSS.escape(id)}"]`).value.trim(),
        }) });
        toast("已使用新会话按新增要求重跑。", "success");
        state.mainDirty = false;
        return refresh();
      }
      if (action === "accept-verification") {
        const evidence = window.prompt("请写明你在目标目录完成了哪些人工检查，以及结果：", "");
        if (!evidence || !evidence.trim()) return;
        const task = await api(`/api/tasks/${state.selectedTaskId}/accept-verification`, {
          method: "POST",
          body: JSON.stringify({ evidence: evidence.trim() }),
        });
        toast(task.status === "completed" ? "人工验收已记录，结果已应用。" : "人工验收已记录，请处理交付提示。", "success");
        return refresh();
      }
      if (action === "message") {
        const id = button.dataset.subtaskId;
        const message = documentRef.querySelector(`[data-subtask-message="${CSS.escape(id)}"]`).value.trim();
        if (!message) throw new Error("请填写要发送给原 Agent 的追加说明。");
        await api(`/api/subtasks/${encodeURIComponent(id)}/message`, {
          method: "POST",
          body: JSON.stringify({ message }),
        });
        state.mainDirty = false;
        toast("追加说明已发送给该子任务的原 Agent。", "success");
        return refresh();
      }
      if (action === "default-repository") {
        const item = state.repositories.find((value) => value.id === button.dataset.repositoryId);
        await api(`/api/repositories/${item.id}`, {
          method: "PUT",
          body: JSON.stringify({ alias: item.alias, isDefault: true }),
        });
      }
      if (action === "initialize-repository") {
        const confirmed = window.confirm("创建初始提交？\n\nFastLab 会把未被 .gitignore 忽略的现有文件加入首次提交。请先确认目录中没有不应提交的敏感文件。");
        if (!confirmed) return;
        await api(`/api/repositories/${button.dataset.repositoryId}/initialize`, { method: "POST", body: "{}" });
        toast("初始提交已创建。", "success");
      }
      if (action === "delete-managed-repository") {
        const item = state.repositories.find(
          (value) => value.id === button.dataset.repositoryId
        );
        if (!item) return;
        if (!window.confirm(
          `永久删除仓库“${item.alias}”？\n\n仓库目录、所有文件、提交和未推送内容都会被删除，相关历史任务也无法再从这个仓库重跑。此操作无法恢复。`,
        )) return;
        await api(`/api/repositories/${item.id}/managed`, { method: "DELETE" });
        toast("托管仓库及磁盘目录已永久删除。", "success");
      }
      await refresh();
    } catch (error) {
      toast(error.message, "error");
    }
  });
}
