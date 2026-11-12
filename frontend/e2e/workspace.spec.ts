import { test, expect } from '@playwright/test';

test.beforeAll(()=>{
  expect(process.env.OPSPILOT_E2E_ISOLATED,'浏览器流程只允许在明确标记的隔离测试环境运行').toBe('1');
});

test('普通用户注册、问题深链接和日志预览',async({page})=>{
  const name='browser_'+Date.now();
  await page.goto('/home');
  await page.getByRole('tab',{name:'注册普通账号'}).click();
  await page.getByLabel('用户名',{exact:true}).fill(name);
  await page.getByLabel('密码',{exact:true}).fill('browser-pass-123');
  await page.getByLabel('确认密码',{exact:true}).fill('browser-pass-123');
  await page.getByRole('button',{name:'注册并进入工作台'}).click();
  await expect(page.getByText('普通用户',{exact:true})).toBeVisible();
  await page.getByRole('menuitem',{name:'问题中心'}).click();
  await page.getByRole('button',{name:'新建问题'}).click();
  await page.getByLabel('问题标题').fill('浏览器验收样例');
  await page.getByRole('button',{name:/确\s*定/}).click();
  await page.getByRole('button',{name:'继续处理'}).first().click();
  await expect(page).toHaveURL(/\/issues\/[a-f0-9]+/);
  await page.reload();
  await page.getByRole('button',{name:'添加日志 / 补充证据'}).click();
  await page.getByLabel('上传日志片段',{exact:true}).setInputFiles({name:'sample.log',mimeType:'text/plain',buffer:Buffer.from('GET /orders HTTP 500\npassword=browser-private')});
  await expect(page.getByRole('button',{name:'确认提交这份证据'})).toBeEnabled();
  await page.getByRole('button',{name:'确认提交这份证据'}).click();
  await expect(page.getByText('证据已保存',{exact:false})).toBeVisible();
  await page.getByLabel('网站排障问题').fill('请分析这个验收样例');
  await page.getByRole('button',{name:'发送给助手'}).click();
  await expect(page.getByText('本次分析已完成',{exact:true})).toBeVisible({timeout:30000});
});

test('390px导航和提问区可见',async({page,request},testInfo)=>{
  const response=await request.post('/auth/register',{data:{username:'mobile_'+Date.now(),password:'mobile-pass-123'}});
  expect(response.ok()).toBeTruthy();const auth=await response.json();
  await page.addInitScript(value=>sessionStorage.setItem('opspilot.auth',JSON.stringify(value)),auth);
  await page.setViewportSize({width:390,height:844});
  await page.goto('/support');
  const box=await page.getByLabel('网站排障问题').boundingBox();
  expect(box?.y).toBeLessThan(844);
  for(const button of await page.locator('.quick-examples button').all()){if(await button.isVisible()){const bounds=await button.boundingBox();expect((bounds?.x||0)+(bounds?.width||0)).toBeLessThanOrEqual(390);}}
  await page.screenshot({path:testInfo.outputPath('mobile-assistant.png'),animations:'disabled'});
  await page.getByRole('button',{name:'打开功能导航'}).click();
  await expect(page.getByRole('menuitem',{name:'我的网站'})).toBeInViewport({ratio:0.9});
  await page.screenshot({path:testInfo.outputPath('mobile-navigation.png'),animations:'disabled'});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
});

test('测试组织工程师核对审批并创建一张工单',async({page,request})=>{
  const admin=await request.post('/auth/login',{data:{username:process.env.OPSPILOT_TEST_ADMIN || 'admin',password:process.env.OPSPILOT_TEST_PASSWORD || 'admin123'}});
  expect(admin.ok()).toBeTruthy();const adminAuth=await admin.json(),username='engineer_browser_'+Date.now(),password='engineer-browser-123';
  const created=await request.post('/api/admin/users',{headers:{Authorization:'Bearer '+adminAuth.access_token},data:{username,password,role:'engineer',tenant_id:adminAuth.tenant_id}});
  expect(created.ok()).toBeTruthy();
  const signed=await request.post('/auth/login',{data:{username,password}}),auth=await signed.json(),headers={Authorization:'Bearer '+auth.access_token};
  const session=await (await request.post('/api/sessions',{headers,data:{title:'审批浏览器验收（非生产）'}})).json();
  const result=await request.post('/api/agent/chat',{headers,data:{session_id:session.id,message:'订单服务ERR_5003连接池耗尽，请创建工单'}});
  expect(result.ok()).toBeTruthy();const answer=await result.json();expect(answer.approval?.approval_id).toBeTruthy();
  await page.addInitScript(value=>sessionStorage.setItem('opspilot.auth',JSON.stringify(value)),auth);
  await page.goto('/operations/'+answer.approval.approval_id);
  await expect(page.getByText('影响范围',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'批准创建工单',exact:true}).click();
  await page.getByRole('button',{name:'确认批准',exact:true}).click();
  await expect(page.getByText('已批准',{exact:true}).first()).toBeVisible();
  const tickets=await (await request.get('/api/tickets',{headers})).json();
  expect(tickets.filter((t:{session_id:string})=>t.session_id===session.id)).toHaveLength(1);
});
