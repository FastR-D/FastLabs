import assert from 'node:assert/strict';
import test from 'node:test';
import { state } from '../../web/modules/state.js';
import { agentOptions, libraryView, assignment, runHistory } from '../../web/modules/agent-library.js';

test('Agent library escapes definitions and filters choices by role', () => {
  state.agents = [{ id:'one', name:'<unsafe>', description:'<script>', version:2, executor:'codex', roles:['worker'] }];
  state.agentDraft = null;
  assert.match(libraryView(), /&lt;unsafe&gt;/);
  assert.doesNotMatch(libraryView(), /<script>/);
  assert.match(agentOptions('worker'), /value="one"/);
  assert.doesNotMatch(agentOptions('planner'), /value="one"/);
});

test('Completed tasks show the frozen definition without an assignment control', () => {
  const html = assignment({ status:'completed', role_settings:{verifier:{agent:{name:'Old name', version:1}}} }, 'verifier');
  assert.match(html, /Old name/);
  assert.doesNotMatch(html, /data-action="agent-assign"/);
  assert.match(html, /agent-save-snapshot/);
});

test('Run history keeps separate sessions and renders errors safely', () => {
  const html = runHistory([
    {role:'worker',status:'failed',executor:'codex',instance_id:'a',session_id:'s1',error:'<error>'},
    {role:'worker',status:'succeeded',executor:'codex',instance_id:'a',session_id:'s2'},
  ]);
  assert.match(html, /s1/);
  assert.match(html, /s2/);
  assert.match(html, /&lt;error&gt;/);
});
