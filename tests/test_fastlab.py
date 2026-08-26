import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_adapter import (
    ClaudeCLIAdapter, ClaudePlanner, CodexCLIAdapter, CodexPlanner,
    PlannerError, parse_json_object, validate_json_schema,
)
from fake_adapter import FakeAgentAdapter
from feishu_gateway import FeishuGateway
from server import (
    FastLab, FastLabHandler, RepositoryInitializationRequired, Store,
    ThreadingHTTPServer, executable_command, load_local_env,
    parse_agent_timeout, parse_claude_extra_allowed_tools, process_group_options,
    resolve_executable,
)


ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX = ROOT / "tests" / "fake_codex.py"
FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"
PLANNER_SCHEMA = ROOT / "schemas" / "planner.schema.json"
PLANNER_SKILL = ROOT / "skills" / "orchestration" / "SKILL.md"


def cli_planner(adapter, backend="codex", model="fake-fast"):
    planner_type = CodexPlanner if backend == "codex" else ClaudePlanner
    return planner_type(adapter, model, PLANNER_SCHEMA, PLANNER_SKILL, effort="high")


def git(repo, *arguments):
    result = subprocess.run(
        ["git", *arguments], cwd=repo, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise AssertionError(result.stdout)
    return result.stdout.strip()


def branch_exists(repo, branch):
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/%s" % branch],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).returncode == 0


def initialize_repo(path, heading="Fixture"):
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / "README.md").write_text("# %s\n" % heading, encoding="utf-8")
    git(path, "add", "README.md")
    subprocess.run(
        ["git", "-c", "user.name=FastLab Test", "-c", "user.email=test@fastlab.local",
         "commit", "-m", "fixture"], cwd=path, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return path


class FastLabIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fastlab-test-")
        self.root = Path(self.temp.name)
        self.repo = initialize_repo(self.root / "repo")
        self.data = self.root / "data"
        self.adapter = FakeAgentAdapter()
        self.app = FastLab(
            self.repo, self.data, agent_adapter=self.adapter, claude_bin=FAKE_CLAUDE,
            planner=cli_planner(self.adapter),
        )

    def tearDown(self):
        for key in (
            "FASTLAB_FAKE_DELAY", "FASTLAB_FAKE_FAIL_KEY", "FASTLAB_FAKE_RAISE_KEY",
            "FASTLAB_FAKE_OUTSIDE_SCOPE", "FASTLAB_FAKE_TRACE",
            "FASTLAB_FAKE_VERIFY_ERROR", "FASTLAB_FAKE_VERIFY_UNCLEAR",
            "FASTLAB_AGENT_TIMEOUT",
        ):
            os.environ.pop(key, None)
        self.app.shutdown()
        self.temp.cleanup()

    def wait_for_status(self, task_id, expected, timeout=18):
        expected = {expected} if isinstance(expected, str) else set(expected)
        deadline = time.time() + timeout
        task = None
        while time.time() < deadline:
            task = self.app.task_payload(task_id)
            if task["status"] in expected:
                return task
            time.sleep(0.06)
        self.fail("task=%s status=%s error=%s subtasks=%s" % (
            task_id, task and task["status"], task and task.get("error"),
            task and [(item["plan_key"], item["status"], item.get("error"))
                      for item in task["subtasks"]],
        ))

    def create_planned_task(self, goal="Build fixture", concurrency=3):
        task = self.app.create_task("Fixture task", goal, "Keep main unchanged", concurrency)
        return self.wait_for_status(task["id"], "awaiting_approval")

    def prepare_task(self, goal="Build fixture", concurrency=3, verifier="codex"):
        task = self.create_planned_task(goal, concurrency)
        return self.app.update_task_verifier(
            task["id"], verifier, model="fake-deep" if verifier == "codex" else "fake-claude"
        )

    def test_planning_uses_fixed_orchestration_skill_and_assigns_executors(self):
        base = git(self.repo, "rev-parse", "HEAD")
        task = self.create_planned_task()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(all(item["executor"] == "codex" for item in task["subtasks"]))
        self.assertNotIn("agent_profile_id", json.dumps(task))
        self.assertNotIn("profileId", json.dumps(task))
        planner = next(call for call in self.adapter.calls if "planner" in call["key"])
        self.assertEqual(planner["sandbox"], "read_only")
        self.assertEqual(planner["model"], "fake-fast")
        self.assertEqual(planner["skills"], [])
        self.assertIn("# FastLab Orchestration", planner["prompt"])
        self.assertIn("任务分配 Skill", task["documents"]["task.md"])
        self.assertNotIn("推荐 Skills", task["documents"]["task.md"])
        with self.assertRaisesRegex(ValueError, "验收执行器"):
            self.app.start_task(task["id"])

    def test_generated_subtask_content_and_executor_can_be_edited_before_start(self):
        task = self.create_planned_task()
        first = task["subtasks"][0]
        updated = self.app.update_subtask_plan(
            first["id"], "生成精简结果", "只创建精简结果并运行对应检查。",
            "codex", model="fake-deep", effort="xhigh",
        )
        edited = next(item for item in updated["subtasks"] if item["id"] == first["id"])
        planned = next(
            item for item in updated["plan"]["subtasks"]
            if item["key"] == first["plan_key"]
        )
        self.assertEqual(edited["title"], "生成精简结果")
        self.assertEqual(edited["instructions"], "只创建精简结果并运行对应检查。")
        self.assertEqual(edited["model"], "fake-deep")
        self.assertEqual(edited["reasoning_effort"], "xhigh")
        self.assertEqual(planned["title"], edited["title"])
        self.assertEqual(planned["instructions"], edited["instructions"])
        self.assertEqual(planned["executor"], "codex")
        self.assertIn("生成精简结果", updated["documents"]["task.md"])
        self.assertIn("只创建精简结果", updated["documents"]["task.md"])

    def test_full_execution_delivers_verified_result_to_target_directory(self):
        task = self.prepare_task()
        base = git(self.repo, "rev-parse", "main")
        self.app.start_task(task["id"])
        completed = self.wait_for_status(task["id"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), base)
        self.assertEqual(
            git(self.repo, "rev-parse", "main"),
            completed["delivered_commit"],
        )
        self.assertTrue(completed["cleaned_at"])
        self.assertFalse(branch_exists(self.repo, completed["integration_branch"]))
        self.assertFalse((self.data / "worktrees" / task["id"]).exists())
        self.assertEqual(completed["delivered_commit"], git(self.repo, "rev-parse", "main"))
        for key in ("S1", "S2", "S3"):
            self.assertIn(key, (self.repo / ("result-%s.txt" % key)).read_text())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertIn("[x]", completed["documents"]["acceptance.md"])
        self.assertIn("已交付", completed["documents"]["progress.md"])
        dispatch_events = [
            event for event in completed["events"]
            if event["kind"] == "subtask.started"
        ]
        self.assertEqual(len(dispatch_events), 3)
        for event in dispatch_events:
            self.assertEqual(event["payload"]["task_id"], task["id"])
            self.assertEqual(event["payload"]["subtask_id"], event["subtask_id"])
            self.assertTrue(event["payload"]["dispatch_id"])
        self.assertIn("当前 Dispatch", completed["documents"]["task.md"])

    def test_delivery_preserves_dirty_target_and_can_be_retried(self):
        task = self.prepare_task()
        base = git(self.repo, "rev-parse", "main")
        os.environ["FASTLAB_FAKE_DELAY"] = "0.16"
        self.app.start_task(task["id"])
        user_file = self.repo / "user-change.txt"
        user_file.write_text("do not overwrite\n", encoding="utf-8")

        attention = self.wait_for_status(task["id"], "needs_attention")
        self.assertIn("未提交改动", attention["error"])
        self.assertTrue(attention["plan"]["verification"]["passed"])
        self.assertIsNone(attention["delivered_commit"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), base)
        self.assertEqual(user_file.read_text(encoding="utf-8"), "do not overwrite\n")

        user_file.unlink()
        delivered = self.app.deliver_task(task["id"])
        self.assertEqual(delivered["status"], "completed")
        self.assertEqual(delivered["delivered_commit"], git(self.repo, "rev-parse", "main"))
        self.assertTrue((self.repo / "result-S1.txt").is_file())

    def test_delivery_reconciles_new_target_commit_and_reverifies(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_DELAY"] = "0.16"
        self.app.start_task(task["id"])
        user_file = self.repo / "user-change.txt"
        user_file.write_text("keep this user commit\n", encoding="utf-8")

        attention = self.wait_for_status(task["id"], "needs_attention")
        self.assertIn("未提交改动", attention["error"])
        git(self.repo, "add", "user-change.txt")
        git(
            self.repo,
            "-c", "user.name=FastLab User",
            "-c", "user.email=user@fastlab.local",
            "commit", "-m", "user change while task runs",
        )
        user_commit = git(self.repo, "rev-parse", "HEAD")

        rechecking = self.app.deliver_task(task["id"])
        self.assertEqual(rechecking["status"], "verifying")
        self.assertNotIn("verification", rechecking["plan"])

        delivered = self.wait_for_status(task["id"], "completed")
        delivered_commit = delivered["delivered_commit"]
        self.assertEqual(delivered_commit, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep this user commit\n")
        self.assertTrue((self.repo / "result-S1.txt").is_file())
        self.assertEqual(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", user_commit, delivered_commit],
                cwd=self.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).returncode,
            0,
        )
        self.assertGreaterEqual(
            len([call for call in self.adapter.calls if "verifier" in call["key"]]),
            2,
        )
        self.assertTrue(any(
            event["kind"] == "task.delivery.reconciled"
            for event in delivered["events"]
        ))

    def test_delivery_conflict_keeps_target_unchanged_and_aborts_merge(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_DELAY"] = "0.16"
        self.app.start_task(task["id"])
        conflicting_file = self.repo / "result-S1.txt"
        conflicting_file.write_text("user version\n", encoding="utf-8")

        attention = self.wait_for_status(task["id"], "needs_attention")
        self.assertIn("未提交改动", attention["error"])
        git(self.repo, "add", "result-S1.txt")
        git(
            self.repo,
            "-c", "user.name=FastLab User",
            "-c", "user.email=user@fastlab.local",
            "commit", "-m", "conflicting user change",
        )
        user_commit = git(self.repo, "rev-parse", "HEAD")
        integration_worktree = Path(attention["integration_worktree"])

        with self.assertRaisesRegex(ValueError, "发生冲突"):
            self.app.deliver_task(task["id"])

        failed_delivery = self.app.task_payload(task["id"])
        self.assertEqual(failed_delivery["status"], "needs_attention")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), user_commit)
        self.assertEqual(conflicting_file.read_text(encoding="utf-8"), "user version\n")
        self.assertTrue(branch_exists(self.repo, failed_delivery["integration_branch"]))
        self.assertTrue(integration_worktree.is_dir())
        self.assertEqual(git(integration_worktree, "status", "--porcelain"), "")
        self.assertNotEqual(
            subprocess.run(
                ["git", "rev-parse", "--quiet", "--verify", "MERGE_HEAD"],
                cwd=integration_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).returncode,
            0,
        )

    def test_repository_without_commits_has_clear_start_error(self):
        unborn = self.root / "unborn"
        unborn.mkdir()
        git(unborn, "init", "-b", "main")
        repository = self.app.store.add_repository("unborn", str(unborn.resolve()))
        task = self.app.create_task(
            "Empty repository", "Create the first feature", "", 1,
            repository_id=repository["id"],
        )
        task = self.wait_for_status(task["id"], "awaiting_approval")
        self.app.update_task_verifier(task["id"], "codex", model="fake-deep")

        with self.assertRaisesRegex(ValueError, "还没有任何提交.*初始提交"):
            self.app.start_task(task["id"])

        unchanged = self.app.task_payload(task["id"])
        self.assertEqual(unchanged["status"], "awaiting_approval")
        self.assertFalse(unchanged.get("integration_worktree"))

        initialized = self.app.initialize_repository(repository["id"])
        self.assertTrue(initialized["initialized"])
        self.assertTrue(initialized["has_commit"])
        self.app.start_task(task["id"])
        self.wait_for_status(task["id"], "completed")

    def test_non_git_repository_requires_confirmation_before_initial_snapshot(self):
        project = self.root / "plain-folder"
        project.mkdir()
        (project / "keep.txt").write_text("keep\n", encoding="utf-8")
        (project / "ignored.txt").write_text("ignore\n", encoding="utf-8")
        (project / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        metadata = project / ".fastlab"
        metadata.mkdir()
        (metadata / "local.env").write_text("secret\n", encoding="utf-8")

        with self.assertRaises(RepositoryInitializationRequired) as required:
            self.app.add_repository("plain", str(project))
        self.assertEqual(required.exception.reason, "not_git")
        self.assertFalse((project / ".git").exists())

        repository = self.app.add_repository("plain", str(project), initialize=True)
        self.assertTrue(repository["initialized"])
        self.assertTrue(repository["has_commit"])
        self.assertEqual(git(project, "log", "-1", "--format=%s"),
                         "Initial snapshot by FastLab")
        tracked = set(git(project, "ls-files").splitlines())
        self.assertIn("keep.txt", tracked)
        self.assertIn(".gitignore", tracked)
        self.assertNotIn("ignored.txt", tracked)
        self.assertNotIn(".fastlab/local.env", tracked)
        self.assertEqual(git(project, "status", "--porcelain"), "")

    def test_rerun_clear_logs_and_delete_task(self):
        task = self.prepare_task()
        self.app.start_task(task["id"])
        completed = self.wait_for_status(task["id"], "completed")
        branch = completed["integration_branch"]
        self.assertFalse(branch_exists(self.repo, branch))
        self.assertTrue(completed["events"])

        rerun = self.app.rerun_task(task["id"])
        self.assertNotEqual(rerun["id"], task["id"])
        rerun = self.wait_for_status(rerun["id"], "awaiting_approval")
        self.assertEqual(rerun["goal"], completed["goal"])

        self.app.clear_task_logs(task["id"])
        self.assertEqual(self.app.task_payload(task["id"])["events"], [])

        task_folder = self.data / "tasks" / task["id"]
        result = self.app.delete_task(task["id"])
        self.assertFalse(result["branchesPreserved"])
        self.assertIsNone(self.app.store.get_task(task["id"]))
        self.assertFalse(task_folder.exists())
        self.assertFalse(branch_exists(self.repo, branch))

        self.app.delete_task(rerun["id"])
        self.assertIsNone(self.app.store.get_task(rerun["id"]))

    def test_continue_task_uses_current_delivered_head_and_keeps_parent_read_only(self):
        task = self.prepare_task()
        self.app.start_task(task["id"])
        parent = self.wait_for_status(task["id"], "completed")
        parent_before = self.app.task_payload(task["id"])

        (self.repo / "after-delivery.txt").write_text("user commit\n", encoding="utf-8")
        git(self.repo, "add", "after-delivery.txt")
        subprocess.run(
            [
                "git", "-c", "user.name=FastLab Test", "-c",
                "user.email=test@fastlab.local", "commit", "-m", "after delivery",
            ],
            cwd=self.repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        continued_head = git(self.repo, "rev-parse", "HEAD")
        child = self.app.continue_task(parent["id"], "请继续增加导出功能")
        child = self.wait_for_status(child["id"], "awaiting_approval")

        self.assertEqual(child["parent_task_id"], parent["id"])
        self.assertTrue(child["title"].startswith("继续："))
        self.assertIn(parent["goal"], child["goal"])
        self.assertIn("请继续增加导出功能", child["goal"])
        self.assertEqual(child["base_commit"], continued_head)
        self.assertEqual(child["base_branch"], "main")
        self.assertEqual(child["repository_id"], parent["repository_id"])
        self.assertEqual(child["working_subdir"], parent["working_subdir"])
        self.assertEqual(child["constraints_text"], parent["constraints_text"])
        self.assertEqual(child["max_concurrency"], parent["max_concurrency"])

        parent_after = self.app.task_payload(parent["id"])
        for key in ("status", "updated_at", "delivered_commit", "plan"):
            self.assertEqual(parent_after[key], parent_before[key])
        with self.assertRaisesRegex(
            ValueError, "原 Agent 现场已清理.*继续修改"
        ):
            self.app.message_subtask(parent["subtasks"][0]["id"], "再改一次")

        self.app.update_task_verifier(child["id"], "codex", model="fake-deep")
        self.app.start_task(child["id"])
        delivered = self.wait_for_status(child["id"], "completed")
        self.assertEqual(delivered["base_commit"], continued_head)
        self.assertEqual(
            (self.repo / "after-delivery.txt").read_text(encoding="utf-8"),
            "user commit\n",
        )

    def test_subtask_resume_reuses_session_worktree_and_creates_new_dispatch(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_DELAY"] = "0.12"
        self.app.start_task(task["id"])
        dirty = self.repo / "keep-dirty.txt"
        dirty.write_text("user\n", encoding="utf-8")
        attention = self.wait_for_status(task["id"], "needs_attention")
        first = attention["subtasks"][0]
        session_id = first["session_id"]
        worktree = first["worktree"]
        old_dispatch = first["dispatch_id"]
        old_model = first["model"]
        old_effort = first["reasoning_effort"]
        dirty.unlink()

        message = "只增加一条精确的补充说明"
        self.app.message_subtask(first["id"], message)
        completed = self.wait_for_status(task["id"], "completed")
        resumed = next(call for call in self.adapter.calls if call["resume"] == session_id)
        current = next(item for item in completed["subtasks"] if item["id"] == first["id"])
        self.assertEqual(resumed["prompt"], message)
        self.assertEqual(resumed["cwd"], worktree)
        self.assertEqual(resumed["model"], old_model)
        self.assertEqual(resumed["effort"], old_effort)
        self.assertNotEqual(current["dispatch_id"], old_dispatch)

    def test_stale_dispatch_cannot_overwrite_current_attempt(self):
        task = self.create_planned_task()
        subtask = task["subtasks"][0]
        self.app.store.update_subtask(
            subtask["id"], status="running", dispatch_id="dispatch-current"
        )
        updated = self.app._update_subtask_state(
            task["id"],
            subtask["id"],
            status="succeeded",
            expected_dispatch_id="dispatch-stale",
        )
        self.assertFalse(updated)
        current = self.app.require_subtask(subtask["id"])[1]
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["dispatch_id"], "dispatch-current")

    def test_rerun_preserves_feishu_source_and_channel_binding(self):
        task = self.app.create_task(
            "Feishu source",
            "Keep channel",
            "",
            1,
            source_channel="feishu",
            channel_context={
                "channel": "feishu",
                "conversationId": "oc_rerun",
                "operatorId": "ou_owner",
                "messageId": "om_source",
            },
        )
        task = self.wait_for_status(task["id"], "awaiting_approval")
        self.app.store.update_task(task["id"], status="cancelled")
        rerun = self.app.rerun_task(task["id"])
        rerun = self.wait_for_status(rerun["id"], "awaiting_approval")
        self.assertEqual(rerun["source_channel"], "feishu")
        bindings = self.app.store.channel_bindings(rerun["id"])
        self.assertEqual(bindings[0]["conversation_id"], "oc_rerun")
        self.assertIsNone(bindings[0]["message_id"])

    def test_feishu_continue_creates_child_and_preserves_notifications(self):
        gateway = FeishuGateway(self.app)
        gateway.handle_command(
            "创建 repo\n目标：从飞书完成首个任务\n并发：2",
            {
                "conversationId": "oc_continue",
                "operatorId": "ou_allowed",
                "messageId": "om_create",
            },
        )
        parent = self.wait_for_status(self.app.list_payload()[0]["id"], "awaiting_approval")
        self.app.update_task_verifier(parent["id"], "codex", model="fake-deep")
        self.app.start_task(parent["id"])
        parent = self.wait_for_status(parent["id"], "completed")
        reply = gateway.handle_command(
            "继续 %s 请增加飞书导出" % parent["id"][:8].upper(),
            {
                "conversationId": "oc_continue",
                "operatorId": "ou_allowed",
                "messageId": "om_continue",
            },
        )
        children = [
            item for item in self.app.list_payload()
            if item.get("parent_task_id") == parent["id"]
        ]
        self.assertEqual(len(children), 1)
        self.assertIn(children[0]["id"][:8].upper(), reply)
        self.assertEqual(children[0]["source_channel"], "feishu")
        bindings = self.app.store.channel_bindings(children[0]["id"])
        self.assertTrue(any(item["conversation_id"] == "oc_continue" for item in bindings))

    def test_cli_agent_timeout_terminates_process_group_and_records_error(self):
        self.app.shutdown()
        timeout_data = self.root / "timeout-data"
        worker = CodexCLIAdapter(str(FAKE_CODEX), timeout_data / "runtime")
        planner_adapter = FakeAgentAdapter()
        self.app = FastLab(
            self.repo,
            timeout_data,
            agent_adapter=worker,
            claude_bin=FAKE_CLAUDE,
            planner=cli_planner(planner_adapter),
        )
        task = self.prepare_task(concurrency=1)
        self.app.agent_timeout = 0.15
        os.environ["FASTLAB_FAKE_DELAY"] = "2"
        self.app.start_task(task["id"])
        stopped = self.wait_for_status(task["id"], "needs_attention", timeout=12)
        self.assertTrue(
            any("运行超过" in (item.get("error") or "") for item in stopped["subtasks"])
        )
        self.assertEqual(worker._active, {})
        self.assertEqual(self.app.health_payload()["agentRuns"]["timeoutSeconds"], 0.15)

    def test_verifier_failure_keeps_the_real_cli_error(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_VERIFY_ERROR"] = "1"
        self.app.start_task(task["id"])
        stopped = self.wait_for_status(task["id"], "needs_attention")
        self.assertIn("验收 Agent 退出，代码 1", stopped["error"])
        self.assertIn("API Error: 402 Insufficient Balance", stopped["error"])
        os.environ.pop("FASTLAB_FAKE_VERIFY_ERROR")
        changed = self.app.update_task_verifier(
            task["id"], "codex", model="fake-deep", effort="high"
        )
        self.assertEqual(changed["role_settings"]["verifier"]["executor"], "codex")
        self.app.verify_task(task["id"])
        self.wait_for_status(task["id"], "completed")

    def test_unclear_verification_uses_disposable_runtime_copy_and_manual_evidence(self):
        task = self.prepare_task()
        base = git(self.repo, "rev-parse", "HEAD")
        os.environ["FASTLAB_FAKE_VERIFY_UNCLEAR"] = "1"
        self.app.start_task(task["id"])
        attention = self.wait_for_status(task["id"], "needs_attention")
        verifier = next(call for call in self.adapter.calls if "verifier" in call["key"])
        self.assertEqual(verifier["sandbox"], "workspace_write")
        self.assertTrue(verifier["local_network"])
        self.assertIn("verification-", verifier["cwd"])
        self.assertFalse(Path(verifier["cwd"]).exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), base)
        self.assertFalse(attention["plan"]["verification"]["passed"])

        completed = self.app.accept_verification(
            task["id"], "已在 Safari 点击主按钮，窄屏和桌面流程均正常。"
        )
        self.assertEqual(completed["status"], "completed")
        reviewed = completed["plan"]["verification"]
        self.assertTrue(reviewed["passed"])
        self.assertIn("manualReview", reviewed)
        manually_passed = next(item for item in reviewed["results"] if item["id"] == "A2")
        self.assertEqual(manually_passed["status"], "passed")
        self.assertEqual(manually_passed["agentStatus"], "unclear")
        self.assertIn("Safari", completed["documents"]["acceptance.md"])

    def test_failed_subtask_can_change_model_before_retry(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_FAIL_KEY"] = "S1"
        self.app.start_task(task["id"])
        attention = self.wait_for_status(task["id"], "needs_attention")
        failed = next(item for item in attention["subtasks"] if item["plan_key"] == "S1")
        os.environ.pop("FASTLAB_FAKE_FAIL_KEY")
        with patch.object(self.app, "_ensure_scheduler"):
            retried = self.app.retry_subtask(
                failed["id"], executor="codex", model="fake-fast", effort="xhigh"
            )
        current = next(item for item in retried["subtasks"] if item["id"] == failed["id"])
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["executor"], "codex")
        self.assertEqual(current["model"], "fake-fast")
        self.assertEqual(current["reasoning_effort"], "xhigh")
        self.assertIsNone(current["session_id"])

    def test_failed_subtask_retry_combines_new_requirement_and_new_model(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_FAIL_KEY"] = "S1"
        self.app.start_task(task["id"])
        attention = self.wait_for_status(task["id"], "needs_attention")
        failed = next(item for item in attention["subtasks"] if item["plan_key"] == "S1")
        os.environ.pop("FASTLAB_FAKE_FAIL_KEY")

        self.app.retry_subtask(
            failed["id"], executor="codex", model="fake-fast", effort="xhigh",
            message="改用更小的结果格式，并补充精确检查。",
        )
        completed = self.wait_for_status(task["id"], "completed")
        retried = next(item for item in completed["subtasks"] if item["id"] == failed["id"])
        call = next(
            item for item in reversed(self.adapter.calls)
            if item["key"].startswith("subtask-") and item["key"].endswith(":S1")
        )
        self.assertEqual(retried["attempt"], 2)
        self.assertEqual(retried["model"], "fake-fast")
        self.assertEqual(retried["reasoning_effort"], "xhigh")
        self.assertIsNone(call["resume"])
        self.assertIn("改用更小的结果格式", call["prompt"])
        self.assertIn("本轮新增要求", call["prompt"])

    def test_succeeded_subtask_can_add_requirement_and_change_model_in_new_session(self):
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_VERIFY_UNCLEAR"] = "1"
        self.app.start_task(task["id"])
        attention = self.wait_for_status(task["id"], "needs_attention")
        first = next(item for item in attention["subtasks"] if item["plan_key"] == "S1")
        old_session = first["session_id"]

        running = self.app.revise_subtask(
            first["id"], "增加导出按钮并补充回归测试。",
            executor="codex", model="fake-fast", effort="xhigh",
        )
        self.assertEqual(running["status"], "running")
        self.assertNotIn("verification", running["plan"])
        finished = self.wait_for_status(task["id"], "needs_attention")
        revised = next(item for item in finished["subtasks"] if item["id"] == first["id"])
        self.assertEqual(revised["attempt"], 2)
        self.assertEqual(revised["model"], "fake-fast")
        self.assertEqual(revised["reasoning_effort"], "xhigh")
        self.assertNotEqual(revised["session_id"], old_session)
        call = next(
            item for item in reversed(self.adapter.calls)
            if item["key"].startswith("subtask-") and item["key"].endswith(":S1")
        )
        self.assertIsNone(call["resume"])
        self.assertEqual(call["model"], "fake-fast")
        self.assertIn("增加导出按钮", call["prompt"])
        self.assertIn("已有实现保留", call["prompt"])
        self.assertTrue(
            (Path(finished["integration_worktree"]) / "revision-S1.txt").is_file()
        )

        self.app.accept_verification(task["id"], "浏览器验证通过。")
        completed = self.app.task_payload(task["id"])
        with self.assertRaisesRegex(ValueError, "现场已清理.*继续修改"):
            self.app.revise_subtask(
                first["id"], "再改一次", executor="codex", model="fake-deep"
            )

    def test_independent_subtasks_really_overlap_in_separate_worktrees(self):
        task = self.prepare_task(concurrency=2)
        self.adapter.calls.clear()
        os.environ["FASTLAB_FAKE_DELAY"] = "0.28"
        self.app.start_task(task["id"])
        self.wait_for_status(task["id"], "completed")
        calls = {call["key"].rsplit(":", 1)[-1]: call for call in self.adapter.calls
                 if call["key"].startswith("subtask-") and call["resume"] is None}
        first, second = calls["S1"], calls["S2"]
        self.assertNotEqual(first["cwd"], second["cwd"])
        self.assertLess(max(first["started_at"], second["started_at"]),
                        min(first["ended_at"], second["ended_at"]))
        self.assertGreaterEqual(self.adapter.max_active, 2)
        third = calls["S3"]
        self.assertGreaterEqual(
            third["started_at"], max(first["ended_at"], second["ended_at"])
        )

    def test_cli_workers_are_distinct_overlapping_processes(self):
        self.app.shutdown()
        cli_data = self.root / "cli-data"
        cli_adapter = CodexCLIAdapter(str(FAKE_CODEX), cli_data / "runtime")
        self.app = FastLab(
            self.repo, cli_data, agent_adapter=cli_adapter, claude_bin=FAKE_CLAUDE,
            planner=cli_planner(cli_adapter, model="fake-model"),
        )
        trace = self.root / "process-trace.jsonl"
        os.environ["FASTLAB_FAKE_TRACE"] = str(trace)
        os.environ["FASTLAB_FAKE_DELAY"] = "0.35"
        task = self.prepare_task(concurrency=2)
        self.app.start_task(task["id"])
        self.wait_for_status(task["id"], "completed")
        records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        by_key = {key: [item for item in records if item["key"] == key]
                  for key in ("S1", "S2")}
        starts = {key: next(item for item in rows if item["event"] == "start")
                  for key, rows in by_key.items()}
        ends = {key: next(item for item in rows if item["event"] == "end")
                for key, rows in by_key.items()}
        self.assertNotEqual(starts["S1"]["pid"], starts["S2"]["pid"])
        self.assertNotEqual(starts["S1"]["cwd"], starts["S2"]["cwd"])
        self.assertLess(max(item["time"] for item in starts.values()),
                        min(item["time"] for item in ends.values()))

    def test_global_limit_caps_multiple_tasks(self):
        self.app.update_executor_settings({"globalConcurrency": 2})
        first = self.prepare_task("First", 4)
        second = self.prepare_task("Second", 4)
        self.adapter.max_active = 0
        os.environ["FASTLAB_FAKE_DELAY"] = "0.18"
        self.app.start_task(first["id"])
        self.app.start_task(second["id"])
        first = self.wait_for_status(first["id"], {"completed", "needs_attention"})
        second = self.wait_for_status(second["id"], {"completed", "needs_attention"})
        self.assertTrue(
            all(item["status"] in {"completed", "needs_attention"}
                for item in (first, second))
        )
        self.assertEqual(self.adapter.max_active, 2)
        self.assertEqual(self.app.health_payload()["agentRuns"]["limit"], 2)

    def test_claude_is_a_fixed_executor_not_a_profile(self):
        task = self.create_planned_task()
        first = task["subtasks"][0]
        updated = self.app.update_subtask_executor(
            first["id"], "claude", model="sonnet", effort="high"
        )
        self.assertEqual(updated["subtasks"][0]["executor"], "claude")
        self.app.update_task_verifier(task["id"], "codex", model="fake-deep")
        self.app.start_task(task["id"])
        completed = self.wait_for_status(task["id"], "completed")
        self.assertIn(
            "fake Claude",
            (self.repo / "claude-S1.txt").read_text(encoding="utf-8"),
        )

    def test_cancel_stops_running_workers(self):
        git(self.repo, "branch", "user-keep")
        task = self.prepare_task()
        os.environ["FASTLAB_FAKE_DELAY"] = "2"
        self.app.start_task(task["id"])
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(item["status"] == "running" for item in self.app.task_payload(task["id"])["subtasks"]):
                break
            time.sleep(0.05)
        self.app.cancel_task(task["id"])
        cancelled = self.wait_for_status(task["id"], "cancelled")
        self.assertTrue(all(item["status"] in {"cancelled", "succeeded"}
                            for item in cancelled["subtasks"]))
        temporary_branches = [cancelled["integration_branch"]]
        temporary_branches.extend(
            item["branch"] for item in cancelled["subtasks"] if item.get("branch")
        )
        self.assertTrue(all(branch_exists(self.repo, branch)
                            for branch in temporary_branches))
        self.assertFalse(cancelled["cleaned_at"])
        cleaned = self.app.cleanup_task_git(task["id"])
        self.assertTrue(cleaned["cleaned_at"])
        self.assertTrue(all(not branch_exists(self.repo, branch)
                            for branch in temporary_branches))
        self.assertTrue(branch_exists(self.repo, "user-keep"))
        self.assertFalse((self.data / "worktrees" / task["id"]).exists())

    def test_failure_conflict_and_retry_preserve_main(self):
        task = self.prepare_task("CONFLICT")
        base = git(self.repo, "rev-parse", "main")
        self.app.start_task(task["id"])
        stopped = self.wait_for_status(task["id"], "needs_attention")
        self.assertTrue(any(item["status"] == "failed" for item in stopped["subtasks"]))
        self.assertEqual(git(self.repo, "rev-parse", "main"), base)
        self.assertEqual(git(Path(stopped["integration_worktree"]), "status", "--porcelain"), "")

    def test_repository_subdir_and_restart_document_consistency(self):
        (self.repo / "web").mkdir()
        (self.repo / "web" / "keep.txt").write_text("scope\n", encoding="utf-8")
        git(self.repo, "add", "web/keep.txt")
        subprocess.run(
            ["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-m", "web"],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
        )
        task = self.app.create_task("Scoped", "Build", "", 2, working_subdir="web\\")
        planned = self.wait_for_status(task["id"], "awaiting_approval")
        self.assertEqual(planned["working_subdir"], "web")
        self.assertIn("工作目录：`web`", planned["documents"]["progress.md"])
        path = self.data / "tasks" / task["id"] / "task.md"
        path.write_text("stale", encoding="utf-8")
        self.app.shutdown()
        restarted_adapter = FakeAgentAdapter()
        self.app = FastLab(
            None, self.data, agent_adapter=restarted_adapter, claude_bin=FAKE_CLAUDE,
            planner=cli_planner(restarted_adapter),
        )
        rebuilt = self.app.task_payload(task["id"])
        self.assertNotIn("stale", rebuilt["documents"]["task.md"])
        with self.assertRaisesRegex(ValueError, "相对目录|不能包含"):
            self.app.create_task("Bad", "Goal", "", 1, working_subdir="../outside")

    def test_out_of_scope_changes_never_merge(self):
        (self.repo / "web").mkdir()
        (self.repo / "web" / "keep.txt").write_text("scope\n", encoding="utf-8")
        git(self.repo, "add", "web/keep.txt")
        subprocess.run(
            ["git", "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-m", "web"],
            cwd=self.repo, check=True, stdout=subprocess.PIPE,
        )
        task = self.app.create_task("Scoped", "Build", "", 2, working_subdir="web")
        planned = self.wait_for_status(task["id"], "awaiting_approval")
        self.app.update_task_verifier(planned["id"], "codex", model="fake-deep")
        os.environ["FASTLAB_FAKE_OUTSIDE_SCOPE"] = "1"
        self.app.start_task(planned["id"])
        stopped = self.wait_for_status(planned["id"], "needs_attention")
        self.assertTrue(any("之外" in (item.get("error") or "") for item in stopped["subtasks"]))
        self.assertFalse((Path(stopped["integration_worktree"]) / "result-S1.txt").exists())

    def test_feishu_commands_use_simple_executor_vocabulary(self):
        gateway = FeishuGateway(self.app)
        reply = gateway.handle_command(
            "创建 repo\n目标：从飞书创建任务\n并发：2",
            {"conversationId": "oc_mobile", "operatorId": "ou_allowed", "messageId": "om_1"},
        )
        self.assertIn("orchestration Skill", reply)
        task = self.wait_for_status(self.app.list_payload()[0]["id"], "awaiting_approval")
        self.assertIn("Codex", gateway.handle_command("执行器", {}))
        short = task["id"][:8].upper()
        self.assertIn("已分配", gateway.handle_command("分配 %s S1 Claude | sonnet | high" % short, {}))
        self.assertIn("最终验收", gateway.handle_command("验收 %s Codex | fake-deep | high" % short, {}))
        card = gateway._task_card(self.app.task_payload(task["id"]))
        labels = [action["text"]["content"] for element in card["body"]["elements"]
                  if element["tag"] == "action" for action in element["actions"]]
        self.assertIn("确认执行", labels)
        help_text = gateway.handle_command("帮助", {})
        self.assertIn("分配 <任务ID>", help_text)
        self.assertNotIn("Profile", help_text)

    def test_http_exposes_new_surface_and_removes_old_management(self):
        FastLabHandler.app = self.app
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), FastLabHandler)
        except PermissionError:
            self.skipTest("sandbox forbids local ports")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:%s" % server.server_address[1]
        try:
            settings = json.loads(urlopen(origin + "/api/settings/executors").read())
            self.assertEqual({item["id"] for item in settings["executors"]}, {"codex", "claude"})
            for path in (
                "/api/settings/agents", "/api/settings/codex", "/api/settings/models",
                "/api/skills",
            ):
                with self.assertRaises(HTTPError) as error:
                    urlopen(origin + path)
                self.assertEqual(error.exception.code, 404)
            request = Request(
                origin + "/api/tasks",
                data=json.dumps({"goal": "Create through HTTP", "maxConcurrency": 2}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            created = json.loads(urlopen(request).read())
            planned = self.wait_for_status(created["id"], "awaiting_approval")
            self.assertTrue(all(item["executor"] for item in planned["subtasks"]))

            self.app.store.update_task(created["id"], status="cancelled")
            rerun_request = Request(
                origin + "/api/tasks/%s/rerun" % created["id"],
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            )
            rerun = json.loads(urlopen(rerun_request).read())
            self.wait_for_status(rerun["id"], "awaiting_approval")

            clear_request = Request(
                origin + "/api/tasks/%s/events" % created["id"], method="DELETE",
            )
            self.assertTrue(json.loads(urlopen(clear_request).read())["ok"])
            self.assertEqual(self.app.task_payload(created["id"])["events"], [])

            for task_id in (created["id"], rerun["id"]):
                delete_request = Request(
                    origin + "/api/tasks/%s" % task_id, method="DELETE",
                )
                self.assertTrue(json.loads(urlopen(delete_request).read())["ok"])
                self.assertIsNone(self.app.store.get_task(task_id))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


class PlannerBackendTests(unittest.TestCase):
    def test_planner_json_parser_accepts_fence_and_enforces_schema(self):
        schema = json.loads(PLANNER_SCHEMA.read_text(encoding="utf-8"))
        plan = FakeAgentAdapter._plan()
        parsed = parse_json_object("```json\n%s\n```" % json.dumps(plan))
        self.assertEqual(validate_json_schema(parsed, schema)["title"], "测试任务")
        with self.assertRaisesRegex(PlannerError, "缺少字段"):
            validate_json_schema({"summary": "invalid"}, schema)

    def test_codex_and_claude_planners_reuse_cli_adapters_in_read_only_mode(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-planners-") as folder:
            root = Path(folder)
            codex = CodexCLIAdapter(str(FAKE_CODEX), root / "runtime")
            codex_result = cli_planner(codex, "codex", "fake-codex").plan(
                "Plan with Codex", {"key": "codex-planner", "cwd": root}
            )
            self.assertEqual(codex_result["title"], "测试任务")

            claude = ClaudeCLIAdapter(str(FAKE_CLAUDE), models=["fake-claude"])
            claude_result = cli_planner(claude, "claude", "fake-claude").plan(
                "Plan with Claude", {"key": "claude-planner", "cwd": root}
            )
            self.assertEqual(claude_result["subtasks"][0]["executor"], "claude")
            self.assertFalse(list(root.glob("claude-S*.txt")))

    def test_claude_plan_executor_launches_claude_worker(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-claude-plan-") as folder:
            root = Path(folder)
            repo = initialize_repo(root / "repo")
            data = root / "data"
            codex = FakeAgentAdapter()
            claude_planner_adapter = ClaudeCLIAdapter(str(FAKE_CLAUDE), models=["fake-claude"])
            app = FastLab(
                repo, data, agent_adapter=codex, claude_bin=FAKE_CLAUDE,
                planner=cli_planner(claude_planner_adapter, "claude", "fake-claude"),
            )
            try:
                task = app.create_task("Claude dispatch", "Use planned executor", "", 1)
                deadline = time.time() + 8
                while time.time() < deadline:
                    task = app.task_payload(task["id"])
                    if task["status"] != "planning":
                        break
                    time.sleep(0.05)
                self.assertEqual(task["subtasks"][0]["executor"], "claude")
                app.update_task_verifier(task["id"], "claude", model="sonnet")
                app.start_task(task["id"])
                deadline = time.time() + 12
                while time.time() < deadline:
                    task = app.task_payload(task["id"])
                    if task["status"] in {"completed", "failed", "needs_attention"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(task["status"], "completed", task.get("error"))
                self.assertIn(
                    "fake Claude",
                    (repo / "claude-S1.txt").read_text(encoding="utf-8"),
                )
            finally:
                app.shutdown()

    def test_missing_cli_keeps_service_available_and_marks_planner_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-missing-cli-") as folder:
            root = Path(folder)
            repo = initialize_repo(root / "repo")
            with patch.dict(os.environ, {
                "FASTLAB_PLANNER_BACKEND": "codex",
                "FASTLAB_CODEX_BIN": str(root / "missing-codex"),
                "FASTLAB_CLAUDE_BIN": str(root / "missing-claude"),
            }, clear=False):
                app = FastLab(repo, root / "data")
                try:
                    self.assertTrue(app.health_payload()["ok"])
                    self.assertFalse(app.health_payload()["planner"]["available"])
                    self.assertTrue(all(not item["available"] for item in app.list_executors()))
                finally:
                    app.shutdown()

    def test_invalid_planner_backend_fails_startup_clearly(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-invalid-planner-") as folder:
            root = Path(folder)
            repo = initialize_repo(root / "repo")
            with patch.dict(os.environ, {"FASTLAB_PLANNER_BACKEND": "not-a-backend"}, clear=False):
                with self.assertRaisesRegex(ValueError, "codex 或 claude"):
                    FastLab(repo, root / "data", codex_bin=FAKE_CODEX)


class ValidationTests(unittest.TestCase):
    def test_existing_database_migrates_parent_and_dispatch_columns(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-migration-") as folder:
            database = Path(folder) / "fastlab.db"
            store = Store(database)
            with store.connect() as db:
                db.execute("ALTER TABLE tasks DROP COLUMN parent_task_id")
                db.execute("ALTER TABLE subtasks DROP COLUMN dispatch_id")
            migrated = Store(database)
            with migrated.connect() as db:
                task_columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(tasks)")
                }
                subtask_columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(subtasks)")
                }
            self.assertIn("parent_task_id", task_columns)
            self.assertIn("dispatch_id", subtask_columns)

    def test_large_and_legacy_event_payloads_remain_valid_json(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-json-recovery-") as folder:
            database = Path(folder) / "fastlab.db"
            store = Store(database)
            task_id = store.create_task(
                "JSON recovery", "Keep logs readable", "", folder, 1
            )
            store.add_event(task_id, "large", "large payload", payload={
                "text": "x" * 50000,
            })
            task = store.get_task(task_id)
            self.assertTrue(task["events"][0]["payload"]["_fastlabTruncated"])

            with store.connect() as db:
                db.execute(
                    "UPDATE events SET payload_json=? WHERE id=?",
                    ('{"broken":"unterminated', task["events"][0]["id"]),
                )
            recovered = Store(database)
            self.assertEqual(recovered.repaired_event_payloads, 1)
            event = recovered.get_task(task_id)["events"][0]
            self.assertTrue(event["payload"]["_fastlabRecovered"])
            with recovered.connect() as db:
                valid = db.execute(
                    "SELECT json_valid(payload_json) FROM events WHERE id=?",
                    (event["id"],),
                ).fetchone()[0]
            self.assertEqual(valid, 1)

    def test_web_poll_preserves_drafts_and_mobile_settings_is_reachable(self):
        app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        index_html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("mainDirty", app_js)
        self.assertIn("canAutoRenderMain", app_js)
        self.assertIn("respectEditing: true", app_js)
        self.assertIn("document.activeElement", app_js)
        self.assertIn("renderVersion", app_js)
        self.assertIn("rememberTaskView", app_js)
        self.assertIn("restoreTaskView", app_js)
        self.assertIn("data-task-scroll", app_js)
        self.assertIn("visibleLogEvents", app_js)
        self.assertIn("function logPresentation(event)", app_js)
        self.assertIn("time: formatLocalTime(sourceTime)", app_js)
        self.assertIn('title="原始时间：', app_js)
        self.assertIn("verifierEditable", app_js)
        self.assertIn("data-subtask-model", app_js)
        self.assertIn("确认并重试", app_js)
        self.assertIn("确认修改并重跑", app_js)
        self.assertIn("执行前编辑", app_js)
        self.assertIn("保存不会启动任务", app_js)
        self.assertIn("data-subtask-requirement", app_js)
        self.assertIn("data-subtask-message", app_js)
        self.assertIn("发送到：", app_js)
        self.assertNotIn('window.prompt("追加什么说明？"', app_js)
        self.assertIn("/revise", app_js)
        self.assertIn("subtask-card\" data-task-panel", app_js)
        self.assertIn("人工确认并应用", app_js)
        self.assertIn('data-action="rerun"', app_js)
        self.assertIn('data-action="continue-task"', app_js)
        self.assertIn("继续修改", app_js)
        self.assertIn('data-action="delete-task"', app_js)
        self.assertIn('data-action="deliver"', app_js)
        self.assertIn('data-action="cleanup-git"', app_js)
        self.assertIn("repository_initialization_required", app_js)
        self.assertIn('data-action="initialize-repository"', app_js)
        self.assertIn('data-action="clear-logs"', app_js)
        self.assertIn('data-task-panel="logs"', app_js)
        self.assertIn('data-task-panel="documents"', app_js)
        self.assertIn('data-action="settings"', index_html)
        self.assertIn(".mobile-settings-button", styles)
        self.assertNotIn('name="plannerModel"', app_js)
        self.assertIn("在 <code>fastlab.env</code> 中修改", app_js)
        self.assertNotIn("一个计划", app_js)
        self.assertNotIn("LOCAL ORCHESTRATION", app_js)
        self.assertNotIn("LOCAL WORKSPACE", index_html)
        self.assertIn('input name="path" required', app_js)
        self.assertNotIn("非 Git 文件夹会先询问，再自动初始化", app_js)
        self.assertNotIn('data-action="login-device"', app_js)
        self.assertNotIn("openai-codex", (ROOT / "requirements.txt").read_text(encoding="utf-8"))

    def test_claude_cli_streams_and_resumes_without_bypass(self):
        adapter = ClaudeCLIAdapter(str(FAKE_CLAUDE), models=["fake-claude"])
        self.assertEqual(
            adapter._message({
                "type": "claude.output",
                "message": "mcpServers: Invalid input",
            }),
            "mcpServers: Invalid input",
        )
        self.assertIsNone(adapter._message({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read"}]},
        }))
        self.assertIsNone(adapter._message({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "done"}]},
        }))
        self.assertIsNone(CodexCLIAdapter._event_message({"type": "turn.started"}))
        with tempfile.TemporaryDirectory(prefix="fastlab-claude-") as folder:
            events = []
            payloads = []
            code, session, _ = adapter.run(
                "claude-test", folder, "子任务 ID：S7\n允许写入范围：整个仓库",
                "workspace_write", model="fake-claude",
                on_event=lambda kind, message, payload: (
                    events.append(kind), payloads.append(payload)
                ),
            )
            self.assertEqual(code, 0)
            init = next(item for item in payloads if item.get("type") == "system")
            self.assertEqual(init["permissionMode"], "auto")
            self.assertFalse(init["dangerousBypass"])
            self.assertTrue(init["settings"]["sandbox"]["enabled"])
            self.assertFalse(
                init["settings"]["sandbox"]["allowUnsandboxedCommands"]
            )
            code, resumed, _ = adapter.run(
                "claude-resume", folder, "子任务 ID：S8\n允许写入范围：整个仓库",
                "workspace_write", resume_session=session,
            )
            self.assertEqual((code, resumed), (0, session))
            self.assertIn("claude.result", events)
            failed_events = []
            code, _, final = adapter.run(
                "claude-failure", folder, "FAIL_PLAIN", "workspace_write",
                on_event=lambda kind, message, payload: failed_events.append(message),
            )
            self.assertEqual(code, 1)
            self.assertEqual(final, "Error: fake Claude startup failed")
            self.assertIn("Error: fake Claude startup failed", failed_events)
            code, _, final = adapter.run(
                "claude-denied", folder, "DENY_COMMAND", "workspace_write"
            )
            self.assertEqual(code, 1)
            self.assertIn("npm run build", final)
            self.assertIn("FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS", final)
            balance_events = []
            balance_payloads = []
            code, _, final = adapter.run(
                "claude-balance", folder, "INSUFFICIENT_BALANCE", "read_only",
                on_event=lambda kind, message, payload: (
                    balance_events.append((kind, message)),
                    balance_payloads.append(payload),
                ),
            )
            self.assertEqual(code, 1)
            self.assertEqual(final, "API Error: 402 Insufficient Balance")
            read_init = next(
                item for item in balance_payloads if item.get("type") == "system"
            )
            self.assertEqual(read_init["permissionMode"], "dontAsk")
            self.assertEqual(read_init["tools"], "Read,Glob,Grep,Bash")
            self.assertNotIn("Edit", read_init["allowedTools"].split(","))
            self.assertNotIn("Write", read_init["allowedTools"].split(","))
            self.assertFalse(read_init["dangerousBypass"])
            self.assertNotIn("user", [message for _, message in balance_events])
            self.assertNotIn("assistant", [message for _, message in balance_events])
            self.assertEqual(
                [message for _, message in balance_events].count(
                    "API Error: 402 Insufficient Balance"
                ),
                1,
            )

    def test_claude_permission_modes_detect_modern_and_fall_back_safely(self):
        parsed = ClaudeCLIAdapter._permission_modes_from_help(
            '--permission-mode <mode> (choices: "acceptEdits", "auto", '
            '"bypassPermissions", "dontAsk", "plan")'
        )
        self.assertEqual(
            parsed, frozenset({"acceptEdits", "auto", "dontAsk", "plan"})
        )
        modern = ClaudeCLIAdapter(
            str(FAKE_CLAUDE), permission_modes=parsed,
        )
        self.assertEqual(modern.read_permission_mode, "dontAsk")
        self.assertEqual(modern.write_permission_mode, "auto")
        self.assertNotIn("bypassPermissions", modern.permission_modes)

        legacy = ClaudeCLIAdapter(
            str(FAKE_CLAUDE),
            permission_modes={"acceptEdits", "plan", "bypassPermissions"},
        )
        self.assertEqual(legacy.read_permission_mode, "plan")
        self.assertEqual(legacy.write_permission_mode, "acceptEdits")
        self.assertNotIn("bypassPermissions", legacy.permission_modes)

    def test_codex_runtime_verifier_allows_only_local_network(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-codex-local-") as folder:
            adapter = CodexCLIAdapter(str(FAKE_CODEX), Path(folder) / "runtime")
            events = []
            code, _, _ = adapter.run(
                "local-verifier", folder, "子任务 ID：S1", "workspace_write",
                local_network=True,
                on_event=lambda kind, message, payload: events.append(message),
            )
            self.assertEqual(code, 0)
            command = next(message for message in events if "network_proxy" in message)
            self.assertIn("sandbox_workspace_write.network_access=true", command)
            self.assertIn('localhost = "allow"', command)
            self.assertIn('"127.0.0.1" = "allow"', command)
            self.assertNotIn("danger-full-access", command)
            self.assertNotIn("dangerously-bypass", command)
            with self.assertRaisesRegex(ValueError, "临时可写 Worktree"):
                adapter.run(
                    "bad-local-verifier", folder, "check", "read_only",
                    local_network=True,
                )

    def test_claude_extra_allowed_tools_are_explicit_and_never_open_all_bash(self):
        self.assertEqual(
            parse_claude_extra_allowed_tools(
                '["Bash(npm run *)", "Bash(python -m ruff *)"]'
            ),
            ("Bash(npm run *)", "Bash(python -m ruff *)"),
        )
        with self.assertRaisesRegex(ValueError, "开放全部 Bash"):
            parse_claude_extra_allowed_tools('["Bash"]')
        with self.assertRaisesRegex(ValueError, "JSON"):
            parse_claude_extra_allowed_tools("Bash(npm run *)")

    def test_local_env_accepts_documented_planner_and_feishu_keys_only(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-env-") as directory:
            path = Path(directory) / "fastlab.env"
            path.write_text(
                "FEISHU_APP_ID=file-app\nFEISHU_APP_SECRET=file-secret\n"
                "FASTLAB_PLANNER_BACKEND=codex\n"
                "FASTLAB_AGENT_TIMEOUT=90\n"
                'FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS=["Bash(npm run *)"]\n'
                "FASTLAB_PLANNER_MODEL=legacy-model-is-ignored\n"
                "FASTLAB_PLANNER_API_KEY=legacy-key-is-ignored\n",
                encoding="utf-8",
            )
            if os.name != "nt": path.chmod(0o600)
            keys = (
                "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FASTLAB_PLANNER_BACKEND",
                "FASTLAB_CLAUDE_EXTRA_ALLOWED_TOOLS", "FASTLAB_AGENT_TIMEOUT",
            )
            legacy = ("FASTLAB_PLANNER_MODEL", "FASTLAB_PLANNER_API_KEY")
            previous = {key: os.environ.pop(key, None) for key in keys + legacy}
            try:
                self.assertEqual(set(load_local_env(path)), set(keys))
                self.assertTrue(all(key not in os.environ for key in legacy))
                bad = Path(directory) / "bad.env"
                bad.write_text("FASTLAB_PROVIDER_UNUSED=secret\n", encoding="utf-8")
                if os.name != "nt": bad.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "不支持"):
                    load_local_env(bad)
            finally:
                for key, value in previous.items():
                    os.environ.pop(key, None)
                    if value is not None: os.environ[key] = value

    def test_command_helpers_support_windows(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-command-") as folder:
            launcher = Path(folder) / "codex.cmd"
            launcher.write_text("@echo off\r\n", encoding="utf-8")
            self.assertEqual(resolve_executable(launcher), str(launcher.resolve()))
        self.assertEqual(executable_command(FAKE_CODEX), [sys.executable, str(FAKE_CODEX)])
        self.assertEqual(process_group_options("nt")["creationflags"], 0x00000200)
        self.assertEqual(parse_agent_timeout("0"), 0)
        self.assertEqual(parse_agent_timeout("12.5"), 12.5)
        with self.assertRaisesRegex(ValueError, "大于等于 0"):
            parse_agent_timeout("-1")

    def test_cycle_and_unknown_executor_are_rejected(self):
        cycle = {
            "title": "Cycle", "summary": "Cycle",
            "subtasks": [
                {"key": "S1", "title": "One", "instructions": "One", "weight": 1,
                 "dependencies": ["S2"], "executor": "codex"},
                {"key": "S2", "title": "Two", "instructions": "Two", "weight": 1,
                 "dependencies": ["S1"], "executor": "codex"},
            ],
            "acceptance": [{"id": "A1", "criterion": "Done"}],
        }
        with self.assertRaisesRegex(ValueError, "循环"):
            FastLab.validate_plan(cycle, {"codex"})
        cycle["subtasks"][0]["dependencies"] = []
        cycle["subtasks"][0]["executor"] = "other"
        with self.assertRaisesRegex(ValueError, "不可用"):
            FastLab.validate_plan(cycle, {"codex"})

    def test_running_records_recover_as_interrupted(self):
        with tempfile.TemporaryDirectory(prefix="fastlab-store-") as folder:
            store = Store(Path(folder) / "test.db")
            task_id = store.create_task("Task", "Goal", "", "/tmp/repo", 1)
            store.update_task(task_id, status="running")
            task = Store(Path(folder) / "test.db").get_task(task_id)
            self.assertEqual(task["status"], "needs_attention")


if __name__ == "__main__":
    unittest.main()
