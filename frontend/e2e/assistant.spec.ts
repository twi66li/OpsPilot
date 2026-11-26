import { expect, test } from '@playwright/test';

test('访客用自带模型聊天、改名并查看本日额度', async ({page}, info) => {
  expect(process.env.OPSPILOT_E2E_ISOLATED).toBe('1');
  const credential = 'test-only-gateway-key';
  const platformBodies: string[] = [];
  const platformAuth: string[] = [];
  page.on('request', request => {
    if (new URL(request.url()).origin === new URL(page.url()).origin) {
      platformBodies.push(request.postData() || '');
      platformAuth.push(request.headers()['authorization'] || '');
    }
  });
  const base = 'https://assistant-gateway.example.test/v1';
  let modelCalls = 0;
  await page.route(base + '/chat/completions', async route => {
    const cors = {'access-control-allow-origin': '*', 'access-control-allow-headers': 'authorization,content-type'};
    if (route.request().method() === 'OPTIONS') return route.fulfill({status: 204, headers: cors});
    modelCalls++;
    expect(route.request().headers().authorization).toBe('Bearer ' + credential);
    expect(route.request().postDataJSON().messages.at(-1).content).toBe('502 一般是什么意思？');
    return route.fulfill({headers: cors, json: {choices: [{message: {content: '代理收到了无效的上游响应。'}}]}});
  });
  await page.setViewportSize({width: 1440, height: 900});
  await page.goto('/home');
  await page.getByRole('button', {name: '访客直接进入', exact: true}).click();
  await page.getByRole('button', {name: '智能维护助手', exact: true}).click();
  await expect(page.getByLabel('给维护助手发消息')).toBeEnabled();
  await page.getByRole('button', {name: '模型设置', exact: true}).click();
  await page.getByLabel('模型 API 基础地址').fill(base);
  await page.getByLabel('模型名称').fill('test-assistant');
  await page.getByLabel(/网关凭据或短期 API Key/).fill(credential);
  await page.getByRole('button', {name: '使用自带模型', exact: true}).click();
  await page.getByLabel('给维护助手发消息').fill('502 一般是什么意思？');
  await page.getByRole('button', {name: '发送', exact: true}).click();
  await expect(page.getByRole('log', {name: '聊天记录'})).toContainText('代理收到了无效的上游响应。');
  expect(modelCalls).toBe(1);
  expect([...platformBodies, ...platformAuth].join(' ')).not.toContain(credential);
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
});
