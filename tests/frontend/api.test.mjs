import assert from "node:assert/strict";
import test from "node:test";

import { api, apiPrefix } from "../../web/modules/api.js";

function response(status, payload) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() {
      return payload;
    },
  };
}

test("apiPrefix normalizes the configured public path", () => {
  assert.equal(apiPrefix({ FASTLAB_PUBLIC_PATH: "/lab/" }), "/lab");
  assert.equal(apiPrefix({ FASTLAB_PUBLIC_PATH: "" }), "");
});

test("api preserves the existing JSON request contract", async () => {
  const calls = [];
  const payload = await api("/api/tasks", { method: "POST", body: "{}" }, {
    window: { FASTLAB_PUBLIC_PATH: "/lab/" },
    fetch: async (...args) => {
      calls.push(args);
      return response(201, { id: "task-1" });
    },
  });
  assert.deepEqual(payload, { id: "task-1" });
  assert.equal(calls[0][0], "/lab/api/tasks");
  assert.deepEqual(calls[0][1], {
    headers: { "Content-Type": "application/json" },
    method: "POST",
    body: "{}",
  });
});

test("api exposes server error code and payload to action handlers", async () => {
  await assert.rejects(
    api("/api/tasks", {}, {
      window: {},
      fetch: async () => response(409, { error: "Conflict", code: "task_conflict" }),
    }),
    (error) => {
      assert.equal(error.message, "Conflict");
      assert.equal(error.code, "task_conflict");
      assert.deepEqual(error.payload, { error: "Conflict", code: "task_conflict" });
      return true;
    },
  );
});

test("api redirects expired FastResearch sessions", async () => {
  const assigned = [];
  await assert.rejects(
    api("/api/me", {}, {
      window: { location: { assign: (url) => assigned.push(url) } },
      fetch: async () => response(401, { loginUrl: "/login?next=%2Flab%2F" }),
    }),
    /FastResearch 登录已失效/,
  );
  assert.deepEqual(assigned, ["/login?next=%2Flab%2F"]);
});

test("api tolerates non-JSON error responses", async () => {
  await assert.rejects(
    api("/api/health", {}, {
      window: {},
      fetch: async () => ({
        status: 503,
        ok: false,
        async json() {
          throw new SyntaxError("not json");
        },
      }),
    }),
    /请求失败（503）/,
  );
});
