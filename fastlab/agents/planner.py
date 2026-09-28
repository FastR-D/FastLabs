"""Read-only structured planners shared by the Codex and Claude backends."""

import json
from pathlib import Path

from .core import PlannerError
from .schema import parse_json_object, validate_json_schema


class Planner:
    """One-shot plan producer. A planner never edits the repository."""

    backend = "unknown"
    kind = "planner"

    def plan(self, prompt, context):
        raise NotImplementedError

    def interrupt(self, key):
        return False

    def health(self):
        return {"backend": self.backend, "kind": self.kind, "available": True}

    def settings(self):
        return self.health()

    def close(self):
        return None


class _SchemaPlanner(Planner):
    def __init__(self, model, schema_path, skill_path, skill_text=None):
        self.model = str(model or "").strip()
        self.schema_path = Path(schema_path).resolve()
        self.skill_path = Path(skill_path).resolve()
        self.schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        self.skill = (
            str(skill_text) if skill_text is not None
            else self.skill_path.read_text(encoding="utf-8")
        )

    def _result(self, value):
        result = parse_json_object(value)
        validate_json_schema(result, self.schema)
        return result

    def _planner_prompt(self, prompt, context):
        public_context = {
            key: _jsonable(value)
            for key, value in (context or {}).items()
            if key not in {"on_event", "key", "schema", "skill"}
        }
        return """You are FastLab's Planner. Produce one execution plan only.
Never edit files, run Git write operations, or start another Agent. Workers must
not create Workers. Assign each subtask to exactly one available executor.
Return only JSON matching the supplied planner schema; do not use Markdown.

FastLab orchestration rules:
{skill}

Request:
{prompt}

Context:
{context}
""".format(
            skill=self.skill,
            prompt=str(prompt),
            context=json.dumps(public_context, ensure_ascii=False, indent=2),
        )


class _CliPlanner(_SchemaPlanner):
    executor = "unknown"

    def __init__(self, adapter, model, schema_path, skill_path, effort=None, skill_text=None):
        super().__init__(model, schema_path, skill_path, skill_text=skill_text)
        self.adapter = adapter
        self.effort = str(effort or "").strip()

    def plan(self, prompt, context):
        if self.adapter is None:
            raise PlannerError(
                "%s Planner 不可用：未找到本地 CLI。" % self.executor.title()
            )
        health = self.adapter.health()
        if not health.get("available", True):
            raise PlannerError(
                "%s Planner 不可用：%s"
                % (self.executor.title(), health.get("error") or "请检查本地 CLI")
            )
        context = context or {}
        key = str(context.get("key") or "fastlab-planner")
        code, session_id, final = self.adapter.run(
            key,
            Path(context.get("cwd") or Path.cwd()),
            self._planner_prompt(prompt, context),
            "read_only",
            schema=self.schema_path,
            on_event=context.get("on_event"),
            model=self.model or None,
            effort=self.effort or None,
            skills=[],
        )
        if code:
            detail = " ".join(str(final or "").split())[:800]
            raise PlannerError(
                "%s Planner 退出，代码 %s。%s"
                % (
                    self.executor.title(),
                    code,
                    (" 原因：%s" % detail) if detail else "",
                )
            )
        if context.get("on_event"):
            context["on_event"](
                "planner.completed",
                "%s Planner 已生成计划。" % self.executor.title(),
                {
                    "backend": self.backend,
                    "model": self.model,
                    "sessionId": session_id,
                },
            )
        return self._result(final)

    def interrupt(self, key):
        return bool(self.adapter and self.adapter.interrupt(key))

    def health(self):
        adapter_health = self.adapter.health() if self.adapter else {}
        return {
            "backend": self.backend,
            "kind": self.kind,
            "model": self.model,
            "available": bool(
                self.adapter and adapter_health.get("available", True)
            ),
            "executable": adapter_health.get("executable"),
            "error": None if self.adapter else "未找到本地 CLI。",
            "source": "environment",
        }


class CodexPlanner(_CliPlanner):
    backend = "codex"
    kind = "codex-cli"
    executor = "codex"


class ClaudePlanner(_CliPlanner):
    backend = "claude"
    kind = "claude-cli"
    executor = "claude"


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    if hasattr(value, "value"):
        return _jsonable(value.value)
    return str(value)


__all__ = ["ClaudePlanner", "CodexPlanner", "Planner", "PlannerError"]
