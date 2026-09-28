import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const webRoot = new URL("../../web/", import.meta.url);

test("browser entry is a native ES module with explicit startup wiring", async () => {
  const [entry, html] = await Promise.all([
    readFile(new URL("app.js", webRoot), "utf8"),
    readFile(new URL("index.html", webRoot), "utf8"),
  ]);
  assert.match(entry, /import \{ start \} from "\.\/modules\/application\.js"/);
  assert.match(entry, /import \{ bindEvents \} from "\.\/modules\/events\.js"/);
  assert.match(entry, /bindEvents\(\);\s*start\(\);/);
  assert.match(html, /<script type="module" src="app\.js"><\/script>/);
});

test("frontend responsibilities remain in separate modules", async () => {
  const modules = ["api", "application", "events", "presentation", "render", "state"];
  const sources = await Promise.all(
    modules.map((name) => readFile(new URL(`modules/${name}.js`, webRoot), "utf8")),
  );
  for (const [index, source] of sources.entries()) {
    assert.ok(source.trim(), `${modules[index]}.js must not be empty`);
  }
  assert.match(sources[0], /export async function api/);
  assert.match(sources[2], /export function bindEvents/);
  assert.match(sources[4], /export async function render/);
  assert.match(sources[5], /export const state/);
});
