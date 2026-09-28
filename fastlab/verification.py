"""Final verification in disposable worktrees."""

import json
import uuid
from pathlib import Path

from .agents import VERIFY_CAPABILITY

from .config import VERIFIER_SCHEMA, compact_error
from .errors import DeliveryReverificationRequired


class VerificationService:
    def __init__(self, application):
        self.application = application

    def __getattr__(self, name):
        return getattr(self.application, name)

    def _verification_workspace(self, task):
        integration = task.get("integration_worktree")
        if integration and Path(integration).is_dir():
            return Path(integration)
        if task.get("delivered_commit"):
            return self._resolve_workspace(task["workspace"])
        raise ValueError("任务集成工作区不存在，无法重新验收。")

    def _create_verification_worktree(self, task):
        """Create a disposable detached copy for runtime verification.

        A verifier may need to create caches or start a localhost server. It
        receives a writable disposable worktree, never the integration branch
        that will be delivered.
        """
        workspace = self._resolve_workspace(task["workspace"])
        commit = task.get("delivered_commit")
        if not commit:
            branch = task.get("integration_branch")
            if not branch:
                raise ValueError("任务集成分支不存在，无法验收。")
            commit = self._git(
                ["rev-parse", "--verify", branch], cwd=workspace
            ).stdout.strip()
        root = self.data_dir / "worktrees" / task["id"]
        root.mkdir(parents=True, exist_ok=True)
        path = root / ("verification-%s" % uuid.uuid4().hex[:10])
        self._git(
            ["worktree", "add", "--detach", str(path), commit], cwd=workspace
        )
        return path

    def _remove_verification_worktree(self, task, path):
        workspace = self._resolve_workspace(task["workspace"])
        result = self._git(
            ["worktree", "remove", "--force", str(path)],
            cwd=workspace,
            check=False,
        )
        if result.returncode:
            self.store.add_event(
                task["id"], "verification.cleanup.warning",
                "临时验收 Worktree 清理失败：%s" % compact_error(result.stdout),
            )

    def _verify_task(self, task_id):
        task = self.require_task(task_id)
        if task["status"] != "verifying":
            self._transition_task(
                task_id, "verifying", "task.verifying", "所有子任务已合并，开始最终验收。"
            )
        criteria = "\n".join(
            "- %s: %s" % (item["id"], item["criterion"])
            for item in task["plan"]["acceptance"]
        )
        prompt = """你是 FastLab 的最终验收 Agent。你在可丢弃的临时验证副本中工作，不得修改实际交付分支，也不得把临时改动当作验收成果。可以运行现有测试；如需验证 Web 运行时，可以只在 localhost 启动临时服务并用项目已有的无头测试或 HTTP 检查验证。不要访问公网。

逐项判断验收标准，并给出来自代码、测试或命令输出的简短证据。先寻找无需浏览器或监听端口的等价自动检查，不要反复执行已被环境明确禁止的命令。如果某项确实只能由真实浏览器人工核验，标记 unclear，并在 evidence 中写出用户可在目标目录执行的准确验证步骤；环境限制不是代码失败。只要存在 failed 或 unclear，passed 必须为 false。results 必须覆盖每个验收 ID，严格输出符合 JSON Schema 的对象。

原始目标：
{goal}

主要工作目录：{working_subdir}

验收标准：
{criteria}
""".format(
            goal=task["goal"],
            working_subdir=task.get("working_subdir") or "仓库根目录",
            criteria=criteria,
        )

        def event(kind, message, payload):
            self.store.add_event(task_id, kind, message, payload=payload)

        try:
            role = task["role_settings"].get("verifier", {})
            executor = role.get("executor")
            if not executor:
                raise ValueError("任务尚未指定最终验收执行器。")
            executor, verifier_adapter = self._executor_adapter(
                executor, VERIFY_CAPABILITY
            )
            verification_worktree = self._create_verification_worktree(task)
            try:
                verified_commit = self._git(
                    ["rev-parse", "--verify", "HEAD"], cwd=verification_worktree
                ).stdout.strip()
                # Codex gets localhost-only networking in a disposable copy.
                # Claude remains in its no-prompt read-only tool profile.
                codex_runtime_check = executor == "codex"
                code, session_id, final = self._run_agent(
                    task_id,
                    "task-%s-verifier" % task_id,
                    verification_worktree,
                    prompt,
                    "workspace_write" if codex_runtime_check else "read_only",
                    executor=executor,
                    capability=VERIFY_CAPABILITY,
                    schema=self._schema_for_adapter(verifier_adapter, VERIFIER_SCHEMA),
                    on_event=event,
                    model=role.get("model") or None,
                    effort=role.get("reasoningEffort") or None,
                    local_network=codex_runtime_check,
                )
            finally:
                self._remove_verification_worktree(task, verification_worktree)
            self.store.update_task(task_id, verifier_thread_id=session_id)
            if code:
                detail = compact_error(final, 800)
                raise RuntimeError(
                    "验收 Agent 退出，代码 %s。%s"
                    % (code, (" 原因：%s" % detail) if detail else "")
                )
            verification = self.parse_json_result(final)
            expected = {item["id"] for item in task["plan"]["acceptance"]}
            actual = {str(item.get("id")) for item in verification.get("results", [])}
            passed = bool(verification.get("passed")) and expected == actual and all(
                item.get("status") == "passed" for item in verification.get("results", [])
            )
            with self._task_lock(task_id):
                current_task = self.require_task(task_id)
                plan = dict(current_task.get("plan") or {})
                plan["verification"] = verification
                plan["verificationCommit"] = verified_commit
                self.store.update_task(task_id, plan_json=json.dumps(plan, ensure_ascii=False))
                if not passed:
                    self._transition_task(
                        task_id, "needs_attention", "task.attention",
                        "最终验收未通过，请查看验收证据。",
                        error=verification.get("summary", "验收未通过。"),
                    )
                elif current_task.get("delivered_commit"):
                    self._transition_task(
                        task_id, "completed", "task.completed",
                        "重新验收通过，成果已在目标目录。",
                        error=None,
                        payload={"deliveredCommit": current_task["delivered_commit"]},
                    )
                else:
                    try:
                        delivered_commit = self._deliver_integration(task_id)
                    except DeliveryReverificationRequired as exc:
                        self._queue_delivery_reverification(task_id, exc)
                        return
                    except Exception as exc:
                        self._transition_task(
                            task_id, "needs_attention", "task.delivery.error",
                            "最终验收通过，但成果未能交付到目标目录：%s"
                            % compact_error(exc),
                            error=compact_error(exc),
                        )
                    else:
                        try:
                            deleted_branches = self._cleanup_task_git(task_id)
                        except Exception as exc:
                            self._transition_task(
                                task_id, "needs_attention", "task.cleanup.error",
                                "成果已交付，但临时 Git 数据清理失败：%s"
                                % compact_error(exc),
                                error=compact_error(exc),
                                payload={"deliveredCommit": delivered_commit},
                            )
                        else:
                            self._transition_task(
                                task_id, "completed", "task.completed",
                                "最终验收通过，成果已交付；临时 Worktree 和分支已清理。",
                                error=None,
                                payload={
                                    "deliveredCommit": delivered_commit,
                                    "deletedBranches": deleted_branches,
                                },
                            )
        except Exception as exc:
            if self.require_task(task_id)["cancel_requested"]:
                self._transition_task(
                    task_id, "cancelled", "task.cancelled", "任务已取消。"
                )
            else:
                self._transition_task(
                    task_id, "needs_attention", "task.error",
                    "最终验收失败：%s" % compact_error(exc), error=compact_error(exc)
                )
