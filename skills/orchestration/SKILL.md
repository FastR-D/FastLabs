---
name: orchestration
description: Split one repository goal into a dependency-aware DAG and assign each independently runnable subtask to Codex or Claude.
---

# FastLab Orchestration

This Skill is FastLab's fixed task allocator. It adapts the DAG and supervised
multi-agent coordination ideas from the public Orca orchestration Skill to
FastLab's JSON plan and native Git worktree scheduler.

Source: https://github.com/stablyai/orca/tree/main/skills/orchestration

The upstream Orca Skill is a discovery stub that requires a live Orca runtime.
FastLab does not claim to run that runtime. Follow the rules below and return a
plan only; FastLab owns process launch, dependency scheduling, worktrees,
merging, cancellation, logs, and verification.

## Planning contract

1. Inspect the repository read-only. Never modify files while planning.
2. Split only where subtasks can produce useful, independently reviewable
   changes. Prefer fewer clear subtasks over artificial fragmentation.
3. Express the plan as a directed acyclic graph. A dependency must mean the
   downstream task cannot safely start before the upstream task is merged.
4. Minimize overlapping file ownership between concurrently runnable tasks.
   If two tasks must edit the same central files, add a dependency or combine
   them.
5. Give every subtask a short title that works as its collapsed summary (at
   most 80 characters). Put the concrete outcome, relevant scope, constraints,
   and checks in `instructions`; keep it specific without repeating the full
   top-level goal. Do not ask a worker to coordinate other workers.
6. Assign exactly one available executor to every subtask:
   - `codex`: default for repository implementation, refactoring, debugging,
     tests, and tasks that need Codex Skills.
   - `claude`: use only when Claude is listed as available and it is a useful
     independent alternative for that subtask.
7. Maximize safe parallelism, not the number of workers. Independent root
   tasks may run simultaneously; dependent tasks wait for successful merges.
8. Acceptance criteria must be observable from code, tests, commands, or the
   final Git diff. Prefer checks that do not require a GUI or public network.
   For UI behavior, include an automated or headless check when practical. Do
   not make time estimates.

Return only the structured plan requested by FastLab.
