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
    await expect(page.getByRole('row').filter({hasText:'大报表导出等待时间过长'})).toBeVisible();
    await expect(page.getByRole('checkbox')).toHaveCount(0);
    await page.getByRole('row').filter({hasText:'示例：结算接口返回 502'}).getByRole('button',{name:'查看记录',exact:true}).click();
    await expect(page.getByLabel('网站排障问题')).toBeDisabled();
    await page.getByRole('button',{name:'补充证据',exact:true}).click();
    await expect(page.getByText('登录个人账号后可上传日志并排查',{exact:true})).toBeVisible();
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
    const {drill_enabled}=await (await request.get('/auth/options')).json();
    await page.goto('/investigation');
    if(drill_enabled){
      await expect(page).toHaveURL(/\/investigation\/visitor-demo-drill-pool-recovery$/);
      await expect(page.locator('.chat-history .chat-message')).toHaveCount(6);
      await expect(page.locator('.chat-history')).toContainText('三次同入口复测通过');
      if(width===1440){
        await expect(page.locator('.evidence-panel')).toContainText('知识引用 · 1');
        await page.getByText(/执行过程 · 6 条记录/).click();
        await expect(page.getByText('周宁批准关闭测试注入，范围为 order-service。')).toBeVisible();
        await page.getByRole('link',{name:'查看审批与三次复测'}).click();
        await expect(page.getByRole('link',{name:'返回演练回放'})).toBeVisible();
        await expect(page.getByText('样本验证通过',{exact:true}).last()).toBeVisible();
        await page.getByRole('link',{name:'返回演练回放'}).click();
        await expect(page).toHaveURL(/\/investigation\/visitor-demo-drill-pool-recovery$/);
      }
    }else await expect(page).toHaveURL(/\/home$/);
    await expect(page.getByText('故障演练 · 独立测试环境，未接入你的网站',{exact:true})).toHaveCount(0);
    if(width===1440){
      await page.getByRole('menuitem',{name:/审批与工单/}).click();
      const pending=page.getByRole('row').filter({hasText:'大报表导出等待时间过长跟进'});
      await expect(pending).toContainText('待审批');
      await pending.getByRole('button',{name:'查看',exact:true}).click();
      await expect(page.getByRole('link',{name:'查看关联问题'})).toBeVisible();
      await expect(page.getByLabel('本次审批意见')).toHaveCount(0);
      await page.goto('/evaluation?run=visitor-eval-2026-08');
      await expect(page.getByText('2026 年 8 月 · 诊断回归（演示）',{exact:true})).toBeVisible();
      await expect(page.getByRole('table')).toContainText('华东同事还看到旧主图');
      await page.goto('/support');
      await page.getByRole('button',{name:/商品图片发布后仍显示旧版本/}).click();
      await expect(page.getByRole('log',{name:'聊天记录'})).toContainText('源站返回新 ETag');
      await expect(page.getByRole('log',{name:'聊天记录'}).locator('time')).toHaveCount(6);
      await expect(page.getByRole('log',{name:'聊天记录'})).toContainText('三个节点的同路径样本一致');
      await expect(page.getByLabel('给维护助手发消息')).toBeDisabled();
      await page.getByRole('button',{name:'新对话',exact:true}).click();
      await expect(page.getByLabel('给维护助手发消息')).toBeEnabled();
    }
    if(width===390){
      await page.goto('/operations');
      await expect(page.getByText('示例审批记录 · 12',{exact:true})).toBeVisible();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
      await page.goto('/evaluation');
      await expect(page.getByText('12 条逐题记录',{exact:true})).toBeVisible();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
    }
    await expect(page.getByRole('button',{name:/^账号：/})).toBeVisible();
    await page.getByRole('button',{name:/^账号：/}).click();
    await page.getByRole('menuitem',{name:/退出访客$/}).click();
    await expect(page.getByRole('button',{name:'访客直接进入',exact:true})).toBeVisible();
  });
}
