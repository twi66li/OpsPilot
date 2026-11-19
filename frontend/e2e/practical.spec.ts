import {test,expect} from '@playwright/test';
test.use({timezoneId:'Asia/Shanghai'});
test.beforeAll(()=>expect(process.env.OPSPILOT_E2E_ISOLATED,'改进流程必须运行在独立测试数据库').toBe('1'));
for(const width of [1440,1024,390]){
 test(`${width}px 网站分组、依赖、变更对照和随页助手`,async({page,request},info)=>{
  const auth=await (await request.post('/auth/register',{data:{username:'practical_'+width+'_'+Date.now(),password:'practical-pass-123'}})).json();
  const headers={Authorization:'Bearer '+auth.access_token};
  await page.addInitScript(value=>sessionStorage.setItem('opspilot.auth',JSON.stringify(value)),auth);
  await page.setViewportSize({width,height:900});await page.goto('/websites');
  await page.getByRole('button',{name:'添加网站',exact:true}).click();
  await page.getByLabel('网站名称',{exact:true}).fill('订单网站 '+width);
  await page.getByLabel('运行环境',{exact:true}).click();await page.locator('.ant-select-dropdown:visible').getByText('生产',{exact:true}).click();
  await page.getByLabel('业务分组',{exact:true}).fill('订单业务');await page.getByLabel('负责人',{exact:true}).fill('维护人');
  await page.getByRole('button',{name:'添加依赖关系',exact:true}).click();
  await page.getByLabel('依赖1使用方',{exact:true}).fill('Nginx');await page.getByLabel('依赖1被依赖方',{exact:true}).fill('app');
  await page.getByRole('button',{name:'添加依赖关系',exact:true}).click();
  await page.getByLabel('依赖2使用方',{exact:true}).fill('app');await page.getByLabel('依赖2被依赖方',{exact:true}).fill('PostgreSQL');
  await page.getByRole('button',{name:'保存网站',exact:true}).click();await expect(page).toHaveURL(/\/websites\/[a-f0-9]+$/);
  const siteId=new URL(page.url()).pathname.split('/')[2];
  const diagram=page.getByLabel('网站依赖关系图');await diagram.getByRole('button',{name:'app',exact:true}).first().click();
  await expect(diagram.locator('.dependency-selection')).toContainText(/PostgreSQL.*Nginx/s);
  await page.screenshot({path:info.outputPath('website-dependencies.png'),fullPage:true,animations:'disabled'});
  await diagram.getByRole('button',{name:'在旁边询问此组件',exact:true}).click();
  const side=page.locator('.side-assistant');await expect(side.getByLabel('网站排障问题')).toHaveValue(/app/);
  await expect(page).toHaveURL('/websites/'+siteId);
  await expect(side.locator('.support-top-actions .ant-select')).toContainText('订单网站 '+width);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  const query='请解释 app 和数据库的关系';await side.getByLabel('网站排障问题').fill(query);
  const streamed=page.waitForRequest(r=>r.url().includes('/api/agent/chat/stream'));
  await side.getByRole('button',{name:'发送',exact:true}).click();
  const body=(await streamed).postDataJSON();
  await expect(side.getByText('本次分析未完成，已有证据保留',{exact:true})).toBeVisible();
  expect((await (await request.get('/api/support/cases/'+body.session_id,{headers})).json()).site_id).toBe(siteId);
  await expect(page).toHaveURL('/websites/'+siteId);
  await side.getByText('分析详情',{exact:true}).click();
  if(width===390){const title=await side.locator('.maintenance-timeline .ant-card-head-title').boundingBox();expect(title!.width).toBeGreaterThan(150);}
  await page.screenshot({path:info.outputPath('side-assistant.png'),fullPage:true,animations:'disabled'});
  await side.locator('.ant-drawer-close').click();
  await page.getByRole('tab',{name:'维护历史',exact:true}).click();
  await page.getByRole('button',{name:'记录网站变更',exact:true}).click();
  await page.getByLabel('变更标题',{exact:true}).fill('发布结算服务 v2');
  await page.getByLabel('发生时间（本机时区）',{exact:true}).fill('2026-11-01T09:55');
  await page.getByLabel('版本 / 镜像（可选）',{exact:true}).fill('v2');
  await page.getByRole('button',{name:'保存变更记录',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'记录已发生的网站变更'})).not.toBeVisible();
  const record=await (await request.post('/api/support/cases',{headers,data:{title:'结算报错',site_id:siteId}})).json();
  await request.post('/api/support/cases/'+record.id+'/evidence',{headers,data:{label:'结算现场日志',text:'2026-11-01T10:00:00+08:00 GET /orders HTTP 502'}});
  await page.reload();const timeline=page.locator('.maintenance-timeline');
  await expect(timeline).toContainText('日志中的时间点');const text=await timeline.innerText();
  expect(text.indexOf('发布结算服务 v2')).toBeLessThan(text.indexOf('结算现场日志'));
  await page.screenshot({path:info.outputPath('change-timeline.png'),fullPage:true,animations:'disabled'});
  await page.goto('/websites');
  await page.getByLabel('按运行环境筛选网站').click();await page.locator('.ant-select-dropdown:visible').getByText('生产',{exact:true}).click();
  const filterRequest=page.waitForRequest(r=>r.url().includes('/api/workspace/sites?')&&new URL(r.url()).searchParams.get('group')==='订单');
  await page.getByLabel('按业务分组筛选网站').fill('订单');await page.getByLabel('按业务分组筛选网站').press('Enter');await filterRequest;
  await expect(page.locator('.home-cards .ant-card-head-title')).toContainText('订单网站 '+width);
  if(width>=1024)await page.getByRole('button',{name:'查看能力状态',exact:true}).click();
  else{await page.locator('.account-button').click();await page.getByRole('menuitem',{name:'查看能力状态',exact:true}).click();}
  await expect(page.locator('.capability-panel')).toContainText(/离线模式.*(?:手动提交|定时 HTTP 巡检).*模型请求未完成/s);
  await page.screenshot({path:info.outputPath('capabilities.png'),fullPage:true,animations:'disabled'});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
 });
}

test('访客可查看新示例且不能记录变更，页面切换会清掉侧边上下文',async({page,request})=>{
 const options=await (await request.get('/auth/options')).json();
 const auth=await (await request.post('/auth/login',{data:options.visitor})).json();
 await page.addInitScript(value=>sessionStorage.setItem('opspilot.auth',JSON.stringify(value)),auth);
 await page.goto('/websites/visitor-example-site');await expect(page.getByLabel('网站依赖关系图')).toContainText('PostgreSQL');
 await page.getByRole('tab',{name:'维护历史',exact:true}).click();
 await expect(page.getByRole('button',{name:'记录网站变更',exact:true})).toBeDisabled();
 await expect(page.locator('.maintenance-timeline')).toContainText('教学示例：发布结算服务 v2');
 await page.getByRole('button',{name:'在旁边提问',exact:true}).click();
 await expect(page.locator('.side-assistant').getByLabel('网站排障问题')).toBeDisabled();
 await page.goto('/websites');await expect(page.locator('.side-assistant')).not.toBeVisible();
});
