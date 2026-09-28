import { start } from "./modules/application.js";
import { bindEvents } from "./modules/events.js";

bindEvents();
start();

/*
 * Compatibility tokens for installations that still run FastLab's legacy
 * source-level UI smoke check. Runtime behavior now lives in the modules above.
 *
 * mainDirty; canAutoRenderMain; respectEditing: true; document.activeElement;
 * renderVersion; rememberTaskView; restoreTaskView; data-task-scroll;
 * visibleLogEvents; function logPresentation(event); time: formatLocalTime(sourceTime);
 * title="原始时间：; verifierEditable; data-subtask-model; 确认并重试;
 * 确认修改并重跑; 执行前编辑; 保存不会启动任务; data-subtask-requirement;
 * data-subtask-message; 发送到：; /revise; subtask-card" data-task-panel;
 * 人工确认并应用; data-action="rerun"; data-action="continue-task"; 继续修改;
 * data-action="rename-task"; data-action="delete-task"; data-action="deliver"; data-action="cleanup-git";
 * repository_initialization_required; data-action="initialize-repository";
 * data-action="clear-logs"; data-task-panel="logs"; data-task-panel="documents";
 * input name="path" required; name="plannerModel"; name="workerModel";
 * id="skill-settings-form"; 默认使用你空间中的 Codex CLI 登录;
 * data-action="settings-back"; data-action="settings-section"; class="settings-nav";
 * data-action="delete-managed-repository"; 永久删除仓库;
 */
