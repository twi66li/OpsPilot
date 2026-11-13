import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { resolve, basename } from 'node:path';
import type { PersonalCase, ChatMessage } from '../src/types';

// 使用真实生产构建和固定接口夹具检查界面，不连接部署环境或调用模型。
test.use({baseURL:'http://opspilot.test'});
const now='2026-11-13T04:00:00Z';
const record:PersonalCase={id:'design-case',title:'结算接口返回 502',status:'investigating',kind:'problem',created_at:now,updated_at:now,site_id:'design-site',site_name:'个人网站',severity:'P1',problem:{endpoint:'/orders'},evidence:[]};
const history:ChatMessage[]=[{role:'user',content:'结算接口返回 502，下一步怎么检查？'},{role:'assistant',content:'先核对同一时间的上游日志。'}];

async function fixture(page:Page,{signedIn=true,role='viewer',item=record,messages=history}:{signedIn?:boolean;role?:string;item?:PersonalCase;messages?:ChatMessage[]}={}) {
  const auth={access_token:'design-fixture',username:'design_user',role,tenant_id:'design'};
  if(signedIn)await page.addInitScript(value=>sessionStorage.setItem('opspilot.auth',JSON.stringify(value)),auth);
  await page.route('**/*',async route=>{
    const req=route.request(),path=new URL(req.url()).pathname;
    if(path.startsWith('/assets/'))return route.fulfill({path:resolve('dist/assets',basename(path))});
    let value:unknown;
    if(path==='/auth/options')value={registration_enabled:true};
    else if(path.startsWith('/auth/'))value=auth;
    else if(path==='/ready')value={status:'ready'};
    else if(path==='/api/workspace/cases')value={items:[item],total:1,page:1,page_size:10};
    else if(path==='/api/workspace/sites')value={items:[{id:'design-site',name:'个人网站'}],total:1,page:1,page_size:6};
    else if(path==='/api/workspace/approvals')value={items:[],total:0,page:1,page_size:1};
    else if(path==='/api/maintenance/todos')value={items:[]};
    else if(path==='/api/support/sites')value=[{id:'design-site',name:'个人网站'}];
    // 真实问题详情不含标题；标题由会话接口返回。
    else if(path==='/api/support/cases'||path===`/api/support/cases/${item.id}`){const {title,...detail}=item;value=detail;}
    else if(path===`/api/sessions/${item.id}`)value={id:item.id,title:item.title,messages};
    else if(path.endsWith('/timeline'))value={events:[{id:1,kind:'case_created',at:now,detail:{}}]};
    else if(path==='/api/agent/runs/design-run/cancel')value={status:'cancel_requested'};
    else if(path==='/api/agent/runs/design-run')value={status:'cancelled'};
    else if(!path.startsWith('/api/'))return route.fulfill({contentType:'text/html',body:await readFile(resolve('dist/index.html'),'utf8')});
    else return route.fulfill({status:404,json:{detail:'未定义的界面测试接口：'+path}});
    return route.fulfill({json:value});
  });
}

async function noOverflow(page:Page) {
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
}

for(const width of [1440,1024,390]) {
  test(`${width}px 登录注册等高与隐私说明`,async({page},info)=>{
    await page.setViewportSize({width,height:900});await fixture(page,{signedIn:false});await page.goto('/home');
    await expect(page.getByRole('tab',{name:'注册',exact:true})).toBeVisible();
    const card=page.locator('.login-card'),login=await card.boundingBox();
    await page.screenshot({path:info.outputPath('login.png')});
    await page.getByRole('tab',{name:'注册',exact:true}).click();
    await expect(page.getByLabel('确认密码',{exact:true})).toBeVisible();
    await expect(page.getByText(/每次注册创建独立私人空间，不会加入其他组织/)).toBeVisible();
    const registration=await card.boundingBox();expect(Math.abs(registration!.height-login!.height)).toBeLessThan(1);
    await page.screenshot({path:info.outputPath('register.png')});
    await page.getByRole('tab',{name:'登录',exact:true}).click();
    await expect(page.getByLabel('确认密码',{exact:true})).toHaveCount(0);
    await expect(page.locator('.login-brand')).toBeVisible();await noOverflow(page);
  });

  test(`${width}px 首页台账和问题深链接`,async({page},info)=>{
    await page.setViewportSize({width,height:900});await fixture(page);await page.goto('/home');
    await expect(page.getByRole('columnheader',{name:'严重度'})).toBeVisible();
    await expect(page.locator('.ledger-problem')).toContainText('/orders');await noOverflow(page);
    await page.screenshot({path:info.outputPath('home.png')});
    await page.getByRole('button',{name:record.title,exact:true}).click();await expect(page).toHaveURL('/issues/design-case');
    await expect(page.getByLabel('网站排障问题')).toBeVisible();
    await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    await page.reload();await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
  });

  test(`${width}px 助手输入区、导航和空状态`,async({page},info)=>{
    await page.setViewportSize({width,height:844});await fixture(page);await page.goto('/support');
    await expect(page.getByRole('heading',{name:'网站遇到了什么问题？'})).toBeVisible();
    await expect(page.locator('.support-suggestions button')).toHaveCount(4);
    const composer=await page.locator('.support-composer').boundingBox();expect(composer!.y+composer!.height).toBeLessThanOrEqual(844);
    await expect(page.getByRole('button',{name:'提交操作后结果',exact:true})).toHaveCount(0);
    await expect(page.getByLabel('网站排障问题')).toHaveAttribute('placeholder',/脱敏后提交/);
    if(width===390){
      await expect(page.locator('.support-conversations')).toBeHidden();
      await expect(page.getByRole('button',{name:'问题',exact:true})).toBeVisible();
      await page.getByRole('button',{name:'打开功能导航'}).click();
      await expect(page.getByRole('menuitem',{name:'我的网站'})).toBeInViewport();
      await expect(page.getByRole('menuitem',{name:'审批与工单'})).toHaveCount(0);
      await page.getByRole('button',{name:'智能维护助手',exact:true}).click();
      await expect(page.locator('.navigation-drawer')).toBeHidden();
    }
    await noOverflow(page);await page.screenshot({path:info.outputPath('assistant.png')});
  });
}

for(const role of ['viewer','engineer','admin']) {
  test(`${role} 导航权限`,async({page})=>{
    await page.setViewportSize({width:1440,height:900});await fixture(page,{role});await page.goto('/home');
    await expect(page.getByRole('menuitem',{name:'我的网站'})).toBeVisible();
    await expect(page.getByRole('button',{name:'智能维护助手',exact:true})).toHaveCount(1);
    await expect(page.getByRole('menuitem',{name:'智能维护助手'})).toHaveCount(0);
    await expect(page.getByRole('menuitem',{name:'审批与工单'})).toHaveCount(role==='viewer'?0:1);
    await expect(page.getByRole('menuitem',{name:'评测记录'})).toHaveCount(role==='admin'?1:0);
    await expect(page.getByText('管理与质量',{exact:true})).toHaveCount(role==='viewer'?0:1);
    await expect(page.getByText('服务已就绪',{exact:true})).toBeVisible();
  });
}

for(const existing of [true,false]) {
  test(`${existing?'已有':'新建'}对话提交证据后保留标题`,async({page})=>{
    await fixture(page,{messages:[]});
    await page.route('**/api/support/evidence/preview',route=>route.fulfill({json:{text:'password=[已脱敏]',replacements:1}}));
    await page.route(`**/api/support/cases/${record.id}/evidence`,route=>route.fulfill({json:{saved:true}}));
    await page.goto(existing?'/support/'+record.id:'/support');
    if(existing)await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    else await page.getByLabel('网站排障问题').fill(record.title!);
    await page.getByRole('button',{name:'添加日志',exact:true}).click();
    await page.getByLabel('证据文本',{exact:true}).fill('password=fictional-test-secret');
    await page.getByRole('button',{name:'脱敏预览',exact:true}).click();
    await expect(page.getByLabel('证据文本',{exact:true})).toHaveValue('password=[已脱敏]');
    const submit=page.getByRole('button',{name:/确认提交这份证据$/});
    await expect(submit).not.toHaveClass(/ant-btn-loading/);
    await submit.click();
    await expect(page.getByText('证据已保存，点击「发送」开始分析。',{exact:true})).toBeVisible();
    await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    await page.reload();await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    await page.getByRole('button',{name:'新对话',exact:true}).click();
    await expect(page.locator('.support-chat-top h3')).toHaveText('新对话');
  });
}

for(const state of ['archived','user_resolved']) {
  test(`${state} 报告、时间线和输入禁用`,async({page},info)=>{
    const item:PersonalCase={...record,status:state==='archived'?'investigating':state,archived:state==='archived',report:{workflow:'diagnose',summary:'已确认上游连接失败，尚需核对原因。',observations:[],hypotheses:['上游进程可能退出'],missing_info:['上游同一时间的日志'],steps:[{location:'服务器',action:'docker logs app',normal:'保留日志',abnormal:'核对退出原因',risk:'只读',rollback:''}],references:[],verification:{verdict:'inconclusive',evidence_ids:[],reason:'证据仍不足'}}};
    await page.setViewportSize({width:390,height:844});await fixture(page,{item});await page.goto('/issues/design-case');
    await expect(page.getByLabel('网站排障问题')).toBeDisabled();await expect(page.getByRole('button',{name:'发送',exact:true})).toBeDisabled();
    await expect(page.locator('.support-report-flow')).toContainText('当前判断');
    await expect(page.locator('.support-chat-scroll .ant-card')).toContainText(['当前判断','建议检查步骤','处理结果核对','维护时间线']);
    await page.getByText('当前判断',{exact:true}).scrollIntoViewIfNeeded();await noOverflow(page);
    await page.screenshot({path:info.outputPath('report.png')});
    await page.getByRole('button',{name:'证据与资料 · 0'}).click();
    await expect(page.getByText('手册是参考依据，不是本次故障的实时证据。')).toBeVisible();
  });
}

test('流式草稿与停止生成',async({page})=>{
  await fixture(page);
  await page.addInitScript(()=>{
    const original=window.fetch;
    window.fetch=async(input,init)=>{
      if(String(input)!=='/api/agent/chat/stream')return original(input,init);
      return new Response(new ReadableStream({start(controller){
        const encoder=new TextEncoder(),emit=(value:unknown)=>controller.enqueue(encoder.encode('data: '+JSON.stringify(value)+'\n\n'));
        emit({type:'phase',workflow:'consult',run_id:'design-run',message:'正在核对当前资料'});
        emit({type:'token',delta:'{"summary":"这是逐步生成的回答草稿',run_id:'design-run'});
        init?.signal?.addEventListener('abort',()=>controller.error(new DOMException('Stopped','AbortError')),{once:true});
      }}),{headers:{'Content-Type':'text/event-stream'}});
    };
  });
  await page.setViewportSize({width:390,height:844});await page.goto('/support/design-case');
  await page.getByLabel('网站排障问题').fill('请解释连接池的作用');await page.getByRole('button',{name:'发送',exact:true}).click();
  await expect(page.getByText('正在核对当前资料',{exact:true})).toBeVisible();await expect(page.locator('.support-draft')).toContainText('逐步生成');
  await expect(page.getByRole('button',{name:'发送',exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'停止生成'}).click();
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeVisible({timeout:6000});
  await expect(page.locator('.support-chat-scroll')).toContainText('先核对同一时间的上游日志。');await noOverflow(page);
});
