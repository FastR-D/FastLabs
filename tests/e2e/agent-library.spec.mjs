import { expect, test } from '@playwright/test';

test('create, copy, edit, assign and archive a reusable Agent', async ({ page, request }) => {
  await page.goto('/');
  await page.getByRole('button', { name:'Agent 库', exact:true }).click();
  await page.getByRole('button', { name:'创建 Agent', exact:true }).click();
  await page.getByLabel('名称', { exact:true }).fill('文档专家 E2E');
  await page.getByLabel('工作指令').fill('保留用户现有内容。');
  await page.getByRole('button', { name:'保存定义', exact:true }).click();
  const card = page.locator('article').filter({ has: page.getByRole('heading', { name:/^文档专家 E2E v[0-9]+$/ }) });
  await expect(card).toBeVisible();
  await card.getByRole('button', {name:'编辑',exact:true}).click();
  await page.getByLabel('职责', {exact:true}).fill('维护项目文档与代码示例。');
  await page.getByRole('button', {name:'保存定义',exact:true}).click();
  await expect(card).toContainText('v2');
  await card.getByRole('button', {name:'复制',exact:true}).click();
  await expect(page.getByLabel('名称', {exact:true})).toHaveValue('文档专家 E2E 副本');
  await page.getByRole('button', {name:'保存定义',exact:true}).click();
  const agents = (await (await request.get('/api/agents')).json()).agents;
  const agent = agents.find(a => a.name === '文档专家 E2E');
  const repoResponse = await request.post('/api/repositories', {data:{alias:'agent-e2e'}});
  expect(repoResponse.ok()).toBeTruthy();
  const repo = await repoResponse.json();
  // Use the real control plane with deterministic CLI fixtures, not mocked HTTP.
  const taskResponse = await request.post('/api/tasks', {data:{goal:'创建结果文件',repositoryId:repo.id,maxConcurrency:1,agents:{worker:agent.id}}});
  expect(taskResponse.ok()).toBeTruthy();
  const task = await taskResponse.json();
  await expect.poll(async () => (await (await request.get(`/api/tasks/${task.id}`)).json()).status).toBe('awaiting_approval');
  await page.reload();
  await page.locator(`[data-task-id="${task.id}"]`).click();
  await page.locator('.subtask-card').first().locator('summary').first().click();
  await expect(page.locator('.subtask-card').first()).toContainText('文档专家 E2E');
  await page.locator('.subtask-card').first().getByRole('button', {name:'编辑任务内定义'}).click();
  await page.getByLabel('名称', {exact:true}).fill('本次文档专家');
  await page.getByRole('button', {name:'保存任务内定义'}).click();
  await expect(page.locator('.subtask-card').first()).toContainText('本次文档专家');
  await page.getByRole('button', {name:'确认并执行',exact:true}).click();
  await expect.poll(async () => (await (await request.get(`/api/tasks/${task.id}`)).json()).status, {timeout:15000}).toBe('completed');
  const finished = await (await request.get(`/api/tasks/${task.id}`)).json();
  expect(finished.runs.some(r => r.role === 'worker')).toBeTruthy();
  await page.getByRole('button', {name:'Agent 库',exact:true}).click();
  page.once('dialog', d => d.accept());
  await card.getByRole('button', {name:'归档',exact:true}).click();
  await expect(card).toHaveCount(0);
  await request.delete(`/api/tasks/${task.id}`);
  await request.delete(`/api/repositories/${repo.id}/managed`);
});

test('Agent editor fits a narrow screen and protects unsaved edits', async ({ page }) => {
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await page.getByRole('button', {name:'Agent 库',exact:true}).click();
  await page.getByRole('button', {name:'创建 Agent',exact:true}).click();
  await page.getByLabel('名称', {exact:true}).fill('未保存定义');
  page.once('dialog', d => d.dismiss());
  await page.getByRole('button', {name:'取消',exact:true}).click();
  await expect(page.getByLabel('名称', {exact:true})).toHaveValue('未保存定义');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});
