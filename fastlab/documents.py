"""Deterministic Markdown projection of task state."""

import os
import uuid
from pathlib import Path

from .config import EXECUTOR_LABELS, ROLE_NAMES


class DocumentProjector:
    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = Path(data_dir)

    def write(self, task_id):
        self._write_docs_unlocked(task_id)

    def _write_docs_unlocked(self, task_id):
        task = self.store.get_task(task_id)
        if not task:
            return
        folder = self.data_dir / "tasks" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        plan = task.get("plan") or {}
        skill = task.get("role_settings", {}).get("skill", {})
        repository = self.store.get_repository(task.get("repository_id")) or {
            "alias": Path(task["workspace"]).name
        }
        subtask_lines = []
        for item in task["subtasks"]:
            dependencies = ", ".join(item["dependencies"]) or "无"
            snapshot = item.get("executor_snapshot") or {}
            subtask_lines.append(
                "### %s · %s\n\n- 依赖：%s\n- 权重：%s\n"
                "- 执行器：`%s`\n- 执行会话：`%s`\n- 当前 Dispatch：`%s`\n"
                "- 模型：`%s`（%s）\n\n%s"
                % (
                    item["plan_key"], item["title"], dependencies, item["weight"],
                    snapshot.get("name") or EXECUTOR_LABELS.get(item.get("executor")) or "尚未指定",
                    item.get("session_id") or "尚未创建",
                    item.get("dispatch_id") or "尚未创建",
                    item.get("model") or "默认",
                    item.get("reasoning_effort") or "默认", item["instructions"],
                )
            )
            definition = snapshot.get('agent')
            if definition:
                subtask_lines[-1] += "\n\n- Agent 定义：%s（%s）" % (
                    definition['name'], 'v%s' % definition['version'] if definition.get('version') else '任务内临时定义')
        role_rows = []
        for role in ROLE_NAMES:
            setting = task["role_settings"].get(role, {})
            if not setting:
                continue
            role_rows.append(
                "- %s：`%s` / `%s`（%s%s）" % (
                    role, setting.get("name") or EXECUTOR_LABELS.get(setting.get("executor")) or "未指定",
                    setting.get("model") or "默认模型",
                    setting.get("reasoningEffort") or "默认推理等级",
                    "；后端 %s" % setting.get("backend") if setting.get("backend") else "",
                )
            )
        task_doc = """# {title}

## 任务目标

{goal}

## 限制条件

{constraints}

## 仓库上下文

- 来源渠道：`{source}`
- 来源任务：`{parent_task}`
- 仓库别名：`{repository_alias}`
- 仓库：`{workspace}`
- 工作目录：`{working_subdir}`
- 基础分支：`{base_branch}`
- 基础提交：`{base_commit}`
- 任务分支：`{integration_branch}`
- 交付提交：`{delivered_commit}`
- 交付时间：`{delivered_at}`
- 临时数据清理：`{cleaned_at}`

## 规划与验收

{role_settings}

- 规划线程：`{planner_thread}`
- 验收线程：`{verifier_thread}`

## 计划摘要

{summary}

## 任务分配 Skill

- `orchestration`
- 来源：`{skill_source}`
- 内容摘要：`{skill_sha256}`
- 运行方式：任务创建时固化的用户 Skill；实际并行由 FastLab Worktree 调度器执行。

## 子任务

{subtasks}
""".format(
            title=task["title"],
            goal=task["goal"],
            constraints=task["constraints_text"] or "无",
            source="web",
            parent_task=task.get("parent_task_id") or "无",
            repository_alias=repository["alias"],
            workspace=task["workspace"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            base_branch=task.get("base_branch") or "尚未确认",
            base_commit=task.get("base_commit") or "尚未确认",
            integration_branch=task.get("integration_branch") or "尚未创建",
            delivered_commit=task.get("delivered_commit") or "尚未交付",
            delivered_at=task.get("delivered_at") or "尚未交付",
            cleaned_at=task.get("cleaned_at") or "尚未清理",
            role_settings="\n".join(role_rows) or "- 验收执行器尚待用户在计划生成后指定。",
            planner_thread=task.get("planner_thread_id") or "尚未创建",
            verifier_thread=task.get("verifier_thread_id") or "尚未创建",
            skill_source="用户定制" if skill.get("customized") else "内置模板",
            skill_sha256=skill.get("sha256") or "旧任务未记录",
            summary=plan.get("summary", "计划生成中。"),
            subtasks="\n\n".join(subtask_lines) or "计划生成中。",
        )
        rows = [
            "| 子任务 | 状态 | 权重 | 尝试次数 | Dispatch |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        for item in task["subtasks"]:
            rows.append(
                "| %s · %s | %s | %s | %s | %s |"
                % (
                    item["plan_key"], item["title"], item["status"],
                    item["weight"], item["attempt"],
                    (item.get("dispatch_id") or "—")[:12],
                )
            )
        progress_doc = """# 任务进度

- 来源渠道：`{source}`
- 仓库别名：`{repository_alias}`
- 工作目录：`{working_subdir}`
- 当前状态：`{status}`
- 完成度：**{progress}%**
- 最后更新：{updated}
- 当前问题：{error}
- 目标目录交付：{delivery}
- 临时数据：{cleanup}

## 子任务状态

{rows}
""".format(
            status=task["status"],
            source="web",
            repository_alias=repository["alias"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            progress=task["progress"],
            updated=task["updated_at"],
            error=task.get("error") or "无",
            delivery=(
                "已交付 `%s`" % task["delivered_commit"]
                if task.get("delivered_commit") else "尚未交付"
            ),
            cleanup="已清理" if task.get("cleaned_at") else "保留中",
            rows="\n".join(rows) if task["subtasks"] else "计划生成中。",
        )
        verification = plan.get("verification") or {}
        result_map = {str(item.get("id")): item for item in verification.get("results", [])}
        acceptance_lines = []
        for item in plan.get("acceptance", []):
            result = result_map.get(item["id"])
            marker = "x" if result and result.get("status") == "passed" else " "
            evidence = "\n  - 证据：%s" % result.get("evidence") if result else ""
            acceptance_lines.append(
                "- [%s] **%s** — %s%s" % (marker, item["id"], item["criterion"], evidence)
            )
        acceptance_doc = """# 验收标准

- 工作目录：`{working_subdir}`
- 验收线程：`{thread}`
- 验收模型：`{model}`（{effort}）
- 目标目录交付：{delivery}
- 临时数据：{cleanup}

{criteria}

## 验收结论

{summary}
""".format(
            criteria="\n".join(acceptance_lines) or "计划生成中。",
            working_subdir=task.get("working_subdir") or "仓库根目录",
            summary=verification.get("summary", "尚未执行最终验收。"),
            thread=task.get("verifier_thread_id") or "尚未创建",
            model=task["role_settings"].get("verifier", {}).get("model") or "默认模型",
            effort=task["role_settings"].get("verifier", {}).get("reasoningEffort") or "默认推理等级",
            delivery=(
                "已交付 `%s`" % task["delivered_commit"]
                if task.get("delivered_commit") else "尚未交付"
            ),
            cleanup="已清理" if task.get("cleaned_at") else "保留中",
        )
        self._atomic_write(folder / "task.md", task_doc)
        self._atomic_write(folder / "progress.md", progress_doc)
        self._atomic_write(folder / "acceptance.md", acceptance_doc)

    @staticmethod
    def _atomic_write(path, content):
        temporary = path.with_suffix(path.suffix + ".%s.tmp" % uuid.uuid4().hex)
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        os.replace(str(temporary), str(path))
