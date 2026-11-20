import {test,expect} from '@playwright/test';

test.beforeAll(()=>expect(process.env.OPSPILOT_E2E_ISOLATED,'访客浏览器检查必须使用独立测试数据库').toBe('1'));
for(const width of [1440,1024,390]){
  test(`${width}px 访客直接登录、查看示例和只读操作`,async({page,request},info)=>{
    await page.setViewportSize({width,height:900});await page.goto('/home');
    const panel=page.getByLabel('访客体验账号');await expect(panel.getByRole('button',{name:'访客直接进入',exact:true})).toBeVisible();
    if(width===1024){
      const account=(await (await request.get('/auth/options')).json()).visitor;
      await page.getByLabel('用户名',{exact:true}).fill(account.username);
      await page.getByLabel('密码',{exact:true}).fill(account.password);
      await page.getByRole('button',{name:'登录',exact:true}).click();
    }else await page.getByRole('button',{name:'访客直接进入',exact:true}).click();
    await expect(page.locator('.home-next')).toContainText('示例：结算接口返回 502');
    await expect(page.getByText('访客只读体验 · 当前是独立教学示例，不是你的真实网站数据',{exact:true})).toHaveCount(0);
    await expect(page.locator('.home-next .ant-btn')).toHaveText('查看记录');
    await page.screenshot({path:info.outputPath('visitor-home.png'),fullPage:true});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
    await page.getByRole('button',{name:/浏览示例问题/}).click();
    await expect(page.getByRole('button',{name:/新建问题/})).toBeDisabled();
    await expect(page.getByRole('button',{name:'查看记录',exact:true})).toHaveCount(2);
    await expect(page.getByRole('checkbox')).toHaveCount(0);
    await page.getByRole('button',{name:'查看记录',exact:true}).first().click();
    await expect(page.getByLabel('网站排障问题')).toBeDisabled();
    await expect(page.getByRole('button',{name:'添加日志',exact:true})).toBeDisabled();
    await expect(page.locator('.support-conversations')).toBeHidden();
    await expect(page.getByRole('button',{name:'删除选中（0）',exact:true})).toHaveCount(0);
    await page.getByRole('button',{name:'证据与资料 · 1',exact:true}).click();
    await page.locator('.ant-drawer-open').getByRole('button',{name:/教学样例日志/}).click();
    await expect(page.getByLabel('日志关键信息')).toContainText('这份日志里有什么');
    await page.screenshot({path:info.outputPath('visitor-evidence.png'),fullPage:true});
    await page.goto('/websites/visitor-example-site');
    await expect(page.getByRole('heading',{name:'示例商城（教学数据）',exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'编辑网站',exact:true})).toBeDisabled();
    await expect(page.getByRole('button',{name:'主动检查HTTP响应',exact:true})).toBeDisabled();
    await page.goto('/knowledge?tab=private');
    await expect(page.getByRole('button',{name:'上传文档',exact:true})).toBeDisabled();
    await page.goto('/knowledge?tab=retrieval');
    await page.getByLabel('检索测试问题').fill('QueuePool 超时');
    await expect(page.getByRole('button',{name:'测试检索',exact:true})).toBeDisabled();
    await page.goto('/investigation');
    await expect(page.getByRole('heading',{name:'体验一次故障调查与恢复',exact:true})).toBeVisible();
    await expect(page.getByText('故障演练 · 独立测试环境，未接入你的网站',{exact:true})).toHaveCount(0);
    await expect(page.getByRole('button',{name:/^账号：/})).toBeVisible();
    await page.getByRole('button',{name:/^账号：/}).click();
    await page.getByRole('menuitem',{name:/退出访客$/}).click();
    await expect(page.getByRole('button',{name:'访客直接进入',exact:true})).toBeVisible();
  });
}
