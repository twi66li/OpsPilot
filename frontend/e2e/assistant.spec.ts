import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';

test('访客用自带模型聊天、改名并查看本日额度', async ({page}, info) => {
  expect(process.env.OPSPILOT_E2E_ISOLATED).toBe('1');
  const credential = 'test-only-gateway-key';
  const platformBodies: string[] = [];
  const leakedKeyPaths: string[] = [];
  page.on('request', request => {
    const parsed = new URL(request.url());
    if (parsed.origin === new URL(page.url()).origin) {
      platformBodies.push(request.postData() || '');
      if (!parsed.pathname.startsWith('/api/agent/chat') && request.headers()['x-opspilot-model-key']) {
        leakedKeyPaths.push(parsed.pathname);
      }
    }
  });

  let modelCalls = 0;
  let receivedSystem = '';
  let receivedUser = '';
  const server = createServer((req, res) => {
    if (req.method !== 'POST' || req.url !== '/v1/chat/completions') {
      res.writeHead(404);
      res.end();
      return;
    }
    const chunks: Buffer[] = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      modelCalls++;
      expect(req.headers.authorization).toBe('Bearer ' + credential);
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      expect(body.model).toBe('test-assistant');
      receivedSystem = body.messages[0]?.content || '';
      receivedUser = body.messages.at(-1)?.content || '';
      const answer = 'OpsPilot 是网站维护工作台。';
      if (body.stream) {
        res.writeHead(200, {'Content-Type': 'text/event-stream'});
        res.write(`data: ${JSON.stringify({id: 'c1', object: 'chat.completion.chunk', choices: [{index: 0, delta: {role: 'assistant', content: answer}, finish_reason: null}]})}\n\n`);
        res.write(`data: ${JSON.stringify({id: 'c1', object: 'chat.completion.chunk', choices: [{index: 0, delta: {}, finish_reason: 'stop'}]})}\n\n`);
        res.write('data: [DONE]\n\n');
        res.end();
      } else {
        res.writeHead(200, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({id: 'c1', object: 'chat.completion', choices: [{index: 0, message: {role: 'assistant', content: answer}, finish_reason: 'stop'}]}));
      }
    });
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as AddressInfo).port;
  const base = `http://127.0.0.1:${port}/v1`;

  try {
    await page.setViewportSize({width: 1440, height: 900});
    await page.goto('/home');
    await page.getByRole('button', {name: '访客直接进入', exact: true}).click();
    await page.getByRole('button', {name: '智能维护助手', exact: true}).click();
    await expect(page.getByLabel('给维护助手发消息')).toBeEnabled();
    await expect(page.locator('.plain-assistant-greeting .plain-assistant-avatar')).toBeVisible();
    await expect(page.locator('.plain-assistant-intro')).toContainText('OpsPilot 智能维护助手');
    await page.getByRole('button', {name: '模型设置', exact: true}).click();
    await expect(page.getByLabel('Base URL')).toHaveValue('https://api.deepseek.com');
    await expect(page.getByLabel('模型 ID')).toHaveValue('deepseek-flash');
    await page.getByRole('button', {name: '使用自带模型', exact: true}).click();
    await expect(page.getByText('请输入 API Key', {exact: true})).toBeVisible();
    await page.getByLabel('Base URL').fill('http://127.0.0.1:11434/v1');
    await page.getByRole('button', {name: '使用自带模型', exact: true}).click();
    await page.getByRole('button', {name: '自带模型', exact: true}).click();
    await page.getByLabel('Base URL').fill(base);
    await page.getByLabel('模型 ID').fill('test-assistant');
    await page.getByLabel('API Key', {exact: true}).fill(credential);
    await page.getByRole('button', {name: '使用自带模型', exact: true}).click();
    await page.getByLabel('给维护助手发消息').fill('介绍下本系统');
    await page.getByRole('button', {name: '发送', exact: true}).click();
    await expect(page.getByRole('log', {name: '聊天记录'})).toContainText('OpsPilot 是网站维护工作台。');
    expect(modelCalls).toBe(1);
    expect(receivedSystem).toContain('OpsPilot');
    expect(receivedUser).toBe('介绍下本系统');
    expect(platformBodies.join(' ')).not.toContain(credential);
    expect(leakedKeyPaths).toEqual([]);
    await page.getByRole('button', {name: /更多操作/}).click();
    await page.getByRole('menuitem', {name: '重命名'}).click();
    await page.getByLabel('对话名称').fill('HTTP 常见错误');
    await page.getByRole('button', {name: '保存', exact: true}).click();
    await expect(page.getByRole('heading', {name: 'HTTP 常见错误'})).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', {name: 'HTTP 常见错误'})).toBeVisible();
    await expect(page.getByRole('button', {name: '模型设置', exact: true})).toBeVisible();
    await page.screenshot({path: info.outputPath('assistant-conversation.png')});
    await page.getByRole('button', {name: /^账号：/}).click();
    await page.getByRole('menuitem', {name: '今日用量'}).click();
    await expect(page.getByRole('dialog', {name: '今日用量'})).toContainText('1 / 3');
    await expect(page.getByRole('dialog', {name: '今日用量'})).toContainText('0 / 50,000');
  } finally {
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});

test('在同一聊天内粘贴日志触发诊断卡片、确认方案并完成复测核对', async ({page, request}) => {
  expect(process.env.OPSPILOT_E2E_ISOLATED).toBe('1');
  const reg = await request.post('/auth/register', {
    data: {username: 'diag_chat_' + Date.now(), password: 'diag-pass-123'},
  });
  expect(reg.ok()).toBeTruthy();
  const auth = await reg.json();
  await page.addInitScript(value => sessionStorage.setItem('opspilot.auth', JSON.stringify(value)), auth);

  let currentCase: Record<string, unknown> | null = null;
  await page.route('**/api/assistant/conversations/**', async route => {
    const url = route.request().url();
    if (url.endsWith('/diagnose') && currentCase && (currentCase as {plan?: unknown}).plan) {
      return route.fulfill({json: {case: currentCase, workflow: 'review', prompt: '请核对复测结果', redaction: {replacements: 0}}});
    }
    if (route.request().method() === 'GET' && currentCase && /\/conversations\/[^/]+$/.test(url)) {
      const real = await route.fetch();
      const data = await real.json();
      const cid = (currentCase as {id: string}).id;
      data.cases = {...(data.cases || {}), [cid]: currentCase};
      return route.fulfill({json: data});
    }
    return route.continue();
  });
  await page.route('**/api/support/cases/**', async route => {
    const url = route.request().url();
    if (url.endsWith('/confirm-plan')) {
      const report = (currentCase as {report: {steps: unknown[]}}).report;
      currentCase = {...currentCase!, status: 'awaiting_results', plan: {revision: 1, evidence_ids: ['E1'], steps: report.steps, checks: [{status: 'pending', note: ''}]}};
      return route.fulfill({json: {status: 'awaiting_results', message: '已确认检查方案'}});
    }
    if (route.request().method() === 'GET' && currentCase) {
      return route.fulfill({json: currentCase});
    }
    return route.continue();
  });

  await page.route('**/api/agent/chat/stream', async route => {
    const body = route.request().postDataJSON();
    if (body.workflow === 'diagnose') {
      currentCase = {
        id: body.session_id,
        title: '下单接口报错',
        status: 'investigating',
        created_at: new Date().toISOString(),
        revision: 1,
        evidence: [{id: 'E1', label: '粘贴的日志片段', kind: 'initial', text: '2026-11-26T10:00:00+08:00 ERROR QueuePool limit\npassword=[已脱敏]\nGET /orders HTTP 502', collected_at: '', submitted_at: ''}],
        report: {
          workflow: 'diagnose',
          summary: '数据库连接池等待超时导致 /orders 返回 502。',
          observations: [{evidence_id: 'E1', line_start: 1, line_end: 1, meaning: '连接池达到上限', quote: 'ERROR QueuePool limit'}],
          hypotheses: [],
          missing_info: [],
          steps: [{location: '应用容器', action: 'docker logs app --tail 50', normal: '无超时报错', abnormal: '仍有连接池等待', risk: '只读', rollback: ''}],
          references: [],
          verification: {verdict: 'inconclusive', evidence_ids: ['E1'], reason: '需复测确认'},
        },
      };
      return route.fulfill({contentType: 'text/event-stream', body: 'data: {"type":"final","end_reason":"answered","answer":"诊断完成"}\n\ndata: {"type":"done"}\n\n'});
    }
    if (body.workflow === 'review') {
      currentCase = {
        ...currentCase!,
        status: 'sample_recovered',
        revision: 2,
        report: {
          ...(currentCase as {report: Record<string, unknown>}).report,
          workflow: 'review',
          summary: '复测样本请求全部 200，未再出现连接池超时。',
          verification: {verdict: 'sample_recovered', evidence_ids: ['E1', 'E2'], reason: '复测样本全部成功'},
        },
      };
      return route.fulfill({contentType: 'text/event-stream', body: 'data: {"type":"final","end_reason":"answered","answer":"复测完成"}\n\ndata: {"type":"done"}\n\n'});
    }
    return route.continue();
  });

  await page.setViewportSize({width: 1440, height: 900});
  await page.goto('/support');

  // 1. 粘贴三行报错日志，出现一键「作为证据诊断」提示栏，不跳页面直接生成诊断卡
  await page.getByLabel('给维护助手发消息').fill(
    '2026-11-26T10:00:00+08:00 ERROR QueuePool limit\npassword=plain-secret\nGET /orders HTTP 502',
  );
  await expect(page.getByRole('button', {name: '作为证据诊断', exact: true})).toBeVisible();
  await page.getByRole('button', {name: '作为证据诊断', exact: true}).click();

  await expect(page).toHaveURL(/\/support\/chat\/[a-f0-9-]+$/);
  const card = page.locator('.chat-diagnosis-card');
  await expect(card).toContainText('数据库连接池等待超时导致 /orders 返回 502。');
  await expect(card).toContainText('已自动遮蔽 1 处敏感信息');

  // 2. 在卡片上确认检查方案，再点单一入口「补充证据」提交复测结果，原地完成核对
  await card.getByRole('button', {name: '确认检查方案', exact: true}).click();
  await expect(card.getByText('未检查', {exact: true})).toBeVisible();
  await card.getByRole('button', {name: '补充证据', exact: true}).click();
  await page.getByLabel('证据文本', {exact: true}).fill('2026-11-26T10:05:00+08:00 GET /orders HTTP 200');
  await page.getByRole('button', {name: '提交并诊断', exact: true}).click();

  await expect(card).toContainText('看起来已恢复');
  await expect(card.getByRole('button', {name: '确认已解决', exact: true})).toBeVisible();
  await page.reload();
  await expect(page.locator('.chat-diagnosis-card')).toContainText('看起来已恢复');
});
