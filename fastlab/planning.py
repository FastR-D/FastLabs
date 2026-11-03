"""Pure planning prompt and plan-validation policy."""

import json
import re
from pathlib import Path

from .config import EXECUTOR_LABELS


class PlanningService:
    @staticmethod
    def schema_for_adapter(adapter, path):
        if getattr(adapter, "kind", "") == "codex-cli":
            return path
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def planner_prompt(task, feedback, available_executors=None):
        extra = "\n用户对上一版计划的反馈：\n%s\n" % feedback if feedback else ""
        selected_executors = (
            set(available_executors) if available_executors is not None else {"codex"}
        )
        executor_lines = "\n".join(
            "- %s" % EXECUTOR_LABELS[item]
            for item in sorted(selected_executors)
        ) or "- 无可用 Worker；请明确报告无法生成可执行计划。"
        return """你是 FastLab 的只读任务规划器。必须遵守已注入的 orchestration Skill。

把目标拆成可以在独立 Git worktree 中完成的任务 DAG。使用 S1、S2… 作为 key，dependencies 只表达真实依赖，weight 取 1 到 5。每个子任务必须从可用执行器中选择 executor；可以用 agentId 选择提供的 Agent，或用 temporaryAgent 创建任务内临时能力定义；两者不可同时使用。简单目标只需一个执行者，不能修改权限或递归派发。title 是折叠时显示的简洁摘要（最多 80 个字符）；instructions 写清具体产出、范围、限制和检查方法，但不要重复整段顶层目标。验收标准必须可验证、可给出证据，优先设计无需 GUI 或公网的自动检查。严格输出符合给定 JSON Schema 的单个 JSON 对象。

目标仓库：{workspace}
主要工作目录：{working_subdir}
任务标题：{title}
任务目标：
{goal}

限制条件：
{constraints}

可用执行器：
{executors}
{extra}""".format(
            workspace=task["workspace"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            title=task["title"],
            goal=task["goal"],
            constraints=task["constraints_text"] or "无",
            executors=executor_lines,
            extra=extra,
        )

    @staticmethod
    def parse_json_result(value):
        text = (value or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text:
            raise ValueError("Agent 没有返回结构化结果。")
        return json.loads(text)

    @staticmethod
    def validate_plan(plan, available_executors=None):
        if not isinstance(plan, dict):
            raise ValueError("任务计划不是 JSON 对象。")
        required = ("title", "summary", "subtasks", "acceptance")
        if any(key not in plan for key in required):
            raise ValueError("任务计划缺少必要字段。")
        subtasks = plan.get("subtasks")
        if not isinstance(subtasks, list) or not 1 <= len(subtasks) <= 20:
            raise ValueError("子任务数量必须在 1 到 20 之间。")
        keys = [str(item.get("key", "")) for item in subtasks]
        if len(set(keys)) != len(keys) or any(not re.match(r"^S[1-9][0-9]*$", key) for key in keys):
            raise ValueError("子任务 key 必须是唯一的 S1、S2… 格式。")
        known = set(keys)
        graph = {}
        allowed_executors = set(available_executors or {"codex"})
        for item in subtasks:
            item["title"] = str(item.get("title", "")).strip()
            item["instructions"] = str(item.get("instructions", "")).strip()
            item["weight"] = max(1, min(5, int(item.get("weight", 1))))
            dependencies = [str(value) for value in item.get("dependencies", [])]
            if not item["title"] or not item["instructions"]:
                raise ValueError("子任务标题和说明不能为空。")
            if len(item["title"]) > 80:
                raise ValueError("子任务标题不能超过 80 个字符。")
            if item["key"] in dependencies or any(value not in known for value in dependencies):
                raise ValueError("子任务依赖引用无效。")
            item["dependencies"] = list(dict.fromkeys(dependencies))
            executor = str(item.get("executor") or "").strip().lower()
            if executor not in allowed_executors:
                raise ValueError("计划选择了不可用的执行器：%s" % (executor or "空"))
            item["executor"] = executor
            item.pop("skills", None)
            item.pop("agentProfile", None)
            graph[item["key"]] = item["dependencies"]
        visiting = set()
        visited = set()

        def visit(node):
            if node in visiting:
                raise ValueError("子任务依赖不能形成循环。")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for key in keys:
            visit(key)
        acceptance = plan.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            raise ValueError("至少需要一条验收标准。")
        ids = set()
        for index, item in enumerate(acceptance, 1):
            item["id"] = str(item.get("id") or "A%s" % index)
            item["criterion"] = str(item.get("criterion", "")).strip()
            if not item["criterion"] or item["id"] in ids:
                raise ValueError("验收标准无效或 ID 重复。")
            ids.add(item["id"])
        plan["title"] = str(plan.get("title", "")).strip()
        plan["summary"] = str(plan.get("summary", "")).strip()
        plan.pop("skills", None)
        plan.pop("verification", None)
        return plan
