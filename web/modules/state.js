export function createInitialState() {
  return {
    tasks: [],
    repositories: [],
    repositoryManagement: { mode: "managed", acceptsPath: false, acceptsCloneUrl: true },
    settings: null,
    health: null,
    me: null,
    skill: null,
    page: "home",
    selectedTaskId: null,
    settingsSection: "agents",
    settingsOrigin: null,
    timer: null,
    mainDirty: false,
    renderVersion: 0,
    taskViews: {},
    openTaskMenuId: null,
  };
}

export const state = createInitialState();

export const statusLabels = Object.freeze({
  planning: "正在规划",
  awaiting_approval: "等待确认",
  running: "执行中",
  verifying: "正在验收",
  completed: "已完成",
  needs_attention: "需要处理",
  failed: "失败",
  cancelled: "已取消",
  pending: "等待",
  succeeded: "已完成",
  blocked: "被阻塞",
});
