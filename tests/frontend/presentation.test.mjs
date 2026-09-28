import assert from "node:assert/strict";
import test from "node:test";

import {
  escapeHtml,
  formatLocalTime,
  logPresentation,
  plannerLabel,
  visibleLogEvents,
} from "../../web/modules/presentation.js";
import { createInitialState, statusLabels } from "../../web/modules/state.js";

test("escapeHtml protects every HTML-sensitive character", () => {
  assert.equal(
    escapeHtml(`<script data-value="'&">`),
    "&lt;script data-value=&quot;&#39;&amp;&quot;&gt;",
  );
  assert.equal(escapeHtml(null), "");
});

test("formatLocalTime keeps empty and invalid timestamps readable", () => {
  assert.equal(formatLocalTime(""), "");
  assert.equal(formatLocalTime("not-a-date"), "not-a-date");
  assert.match(formatLocalTime("2026-01-02T03:04:05Z"), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
});

test("logPresentation separates an embedded UTC timestamp from its message", () => {
  const presented = logPresentation({
    created_at: "2025-01-01T00:00:00Z",
    message: "2026-01-02T03:04:05.123Z Agent started",
  });
  assert.equal(presented.sourceTime, "2026-01-02T03:04:05.123Z");
  assert.equal(presented.message, "Agent started");
  assert.equal(presented.time, formatLocalTime(presented.sourceTime));
});

test("visibleLogEvents hides protocol noise but preserves meaningful entries", () => {
  const visible = visibleLogEvents([
    { kind: "worker.started", message: "started" },
    { kind: "worker.message", message: "worker.message" },
    { kind: "worker.role", message: "assistant" },
    { kind: "worker.output", message: "Changed two files" },
    { kind: "worker.empty", message: "" },
  ]);
  assert.deepEqual(visible, [{ kind: "worker.output", message: "Changed two files" }]);
});

test("state factory returns isolated mutable state and labels remain stable", () => {
  const first = createInitialState();
  const second = createInitialState();
  first.tasks.push({ id: "task-1" });
  first.taskViews.one = { pageY: 20 };
  assert.deepEqual(second.tasks, []);
  assert.deepEqual(second.taskViews, {});
  assert.equal(second.settingsSection, "agents");
  assert.equal(second.settingsOrigin, null);
  assert.equal(second.repositoryManagement.mode, "managed");
  assert.equal(statusLabels.awaiting_approval, "等待确认");
  assert.equal(plannerLabel({ backend: "claude" }), "Claude CLI");
  assert.equal(plannerLabel({ backend: "codex" }), "Codex CLI");
});
