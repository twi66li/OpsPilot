import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { resolve, basename } from 'node:path';
import type { AgentEvent, PersonalCase, ChatMessage, Site } from '../src/types';
import type { EvidenceSummaryData, MaintenanceTodo } from '../src/types';

// 使用真实生产构建和固定接口夹具检查界面，不连接部署环境或调用模型。
test.use({baseURL:'http://opspilot.test'});
const dist = resolve(process.env.OPSPILOT_TEST_DIST || 'dist');
const now='2026-11-13T04:00:00Z';
const record:PersonalCase={id:'design-case',title:'结算接口返回 502',status:'investigating',kind:'problem',created_at:now,updated_at:now,site_id:'design-site',site_name:'个人网站',severity:'P1',problem:{endpoint:'/orders'},evidence:[]};
const history:ChatMessage[]=[{role:'user',content:'结算接口返回 502，下一步怎么检查？'},{role:'assistant',content:'先核对同一时间的上游日志。'}];
const site:Site={id:'design-site',name:'个人网站',url:'https://example.com',framework:'FastAPI',deployment:'Docker Compose',database:'PostgreSQL 16',versions:'Python 3.12',notes:'发布后优先核对结算接口',doc_ids:['design-guide'],problem_count:4,open_count:2};
const siteDetail={...site,documents:[{id:'design-guide',title:'结算服务维护手册',source:'private',chunk_count:12}],cases:[
  record,
  {...record,id:'waiting-case',title:'等待检查数据库连接',status:'awaiting_results'},
  {...record,id:'resolved-case',title:'连接池配置已修复',status:'user_resolved'},
  {...record,id:'archived-case',title:'已归档但尚未确认解决',archived:true},
]};
const diagnosis:PersonalCase={...record,revision:1,evidence:[{id:'E1',label:'已提交的应用日志',kind:'initial',text:Array.from({length:70},(_,index)=>index===44?'QueuePool limit reached':`application log line ${index+1}`).join('\n'),collected_at:now,submitted_at:now}],report:{workflow:'diagnose',summary:'提交日志显示连接等待超时，根因仍需核对。',observations:[{evidence_id:'E1',line_start:45,line_end:46,meaning:'这两行记录了连接等待超时，尚不能证明连接泄漏。',quote:'QueuePool limit reached\napplication log line 46'}],hypotheses:['连接可能没有及时释放'],missing_info:['同一时间的活跃连接数'],steps:[{location:'应用服务器',action:'docker logs app --tail 100',normal:'可看到同一时间的完整请求记录',abnormal:'记录退出原因与错误时间',risk:'只读',rollback:''}],references:[1],verification:{verdict:'inconclusive',evidence_ids:['E1'],reason:'尚未提供操作后的新样本'}},report_hits:[{chunk_id:'official-pool',doc_id:'official-guide',doc_title:'连接等待超时说明',text:'资料用于理解连接等待，不代表本次根因。',source:'https://docs.example.test/pool',source_kind:'official',citation_id:1,checked_at:'2026-11-14',applicable_version:'测试环境'}]};

async function fixture(page:Page,{signedIn=true,role='viewer',tenantId='design',item=record,messages=history,guest=false}:{signedIn?:boolean;role?:string;tenantId?:string;item?:PersonalCase;messages?:ChatMessage[];guest?:boolean}={}) {
  const auth={access_token:'design-fixture',username:'design_user',role,tenant_id:tenantId,is_guest:guest};
  if(signedIn)await page.addInitScript(value=>sessionStorage.setItem('opspilot.auth',JSON.stringify(value)),auth);
  await page.route('**/*',async route=>{
    const req=route.request(),path=new URL(req.url()).pathname;
    if(path.startsWith('/assets/'))return route.fulfill({path:resolve(dist,'assets',basename(path))});
    let value:unknown;
    if(path==='/auth/options')value={registration_enabled:true,drill_enabled:true};
    else if(path.startsWith('/auth/'))value=auth;
    else if(path==='/ready')value={status:'ready'};
    else if(path==='/api/assistant/conversations')value=[];
    else if(path==='/api/workspace/cases')value={items:[item],total:1,page:1,page_size:10};
    else if(path==='/api/workspace/sites')value={items:[site],total:1,page:1,page_size:6};
    else if(path==='/api/workspace/approvals')value={items:[],total:0,page:1,page_size:1};
    else if(path==='/api/maintenance/todos')value={items:[]};
    else if(path==='/api/maintenance/activity')value={events:[]};
    else if(path==='/api/support/sites')value=[site];
    else if(path==='/api/support/scenarios')value=JSON.parse(await readFile(resolve('../app/knowledge/scenarios.json'),'utf8'));
    else if(path==='/api/support/library')value=[{doc_id:'design-guide',title:'连接等待超时指南',product:'SQLAlchemy',applicable_version:'2.x',checked_at:'2026-11-14',url:'https://docs.example.test/pool',content:'核对超时日志与实际连接使用情况。'}];
    else if(path===`/api/support/sites/${site.id}`)value=siteDetail;
    else if(path==='/api/support/private-documents')value=siteDetail.documents;
    // 真实问题详情不含标题；标题由会话接口返回。
    else if(path==='/api/support/cases'||path===`/api/support/cases/${item.id}`){const {title,...detail}=item;value=detail;}
    else if(path===`/api/sessions/${item.id}`)value={id:item.id,title:item.title,messages};
    else if(path.endsWith('/timeline'))value={events:[{id:1,kind:'case_created',at:now,detail:{}}]};
    else if(path==='/api/agent/runs/design-run/cancel')value={status:'cancel_requested'};
    else if(path==='/api/agent/runs/design-run')value={status:'cancelled'};
    else if(!path.startsWith('/api/'))return route.fulfill({contentType:'text/html',body:await readFile(resolve(dist,'index.html'),'utf8')});
    else return route.fulfill({status:404,json:{detail:'未定义的界面测试接口：'+path}});
    return route.fulfill({json:value});
  });
}

async function noOverflow(page:Page) {
  const layout=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,overflow:[...document.querySelectorAll('body *')].filter(node=>{const box=node.getBoundingClientRect();return box.width>0&&box.right>innerWidth+1;}).slice(0,8).map(node=>({class:node.className,right:node.getBoundingClientRect().right}))}));
  expect(layout.scroll,JSON.stringify(layout.overflow)).toBeLessThanOrEqual(layout.width);
}

test('首页局部失败只重试对应区域',async({page})=>{
  await fixture(page);
  let caseLoads=0, activityLoads=0;
  await page.route('**/api/workspace/cases?**',route=>++caseLoads===1
    ?route.fulfill({status:503,json:{detail:'问题暂时不可用'}})
    :route.fulfill({json:{items:[record],total:1,page:1,page_size:6}}));
  await page.route('**/api/maintenance/activity',route=>{
    activityLoads++;
    return route.fulfill({json:{events:[{id:7,kind:'http_incident',at:now,case_id:record.id,
      detail:{title:'旧网站刚发生异常'},siteId:'older-site',siteName:'旧网站'}]}});
  });
  await page.goto('/home');
  await expect(page.getByText('旧网站刚发生异常')).toBeVisible();
  const ledger=page.getByRole('region',{name:'未归档问题'});
  await expect(ledger.getByText('问题加载失败')).toBeVisible();
  await ledger.getByRole('button',{name:'重试'}).click();
  await expect(ledger.getByRole('button',{name:record.title})).toBeVisible();
  expect(activityLoads).toBe(1);
});

test('网站开启定时 HTTP 巡检并从问题详情启动分析',async({page})=>{
  const item:PersonalCase={...record,status:'evidence_submitted',evidence:[{
    id:'probe',label:'定时 HTTP 巡检',kind:'initial',source_kind:'scheduled_http_probe',
    text:'2026-11-18T01:00:00Z GET /orders HTTP/1.1 502',collected_at:now,submitted_at:now,
  }]};
  await fixture(page,{item});
  await page.route('**/api/maintenance/sites/design-site/http-monitor',route=>{
    expect(route.request().postDataJSON()).toEqual({enabled:true,interval_minutes:15});
    return route.fulfill({json:{enabled:true,interval_minutes:15,failure_threshold:3,case_id:item.id}});
  });
  await page.route('**/api/agent/chat/stream',route=>{
    expect(route.request().postDataJSON()).toMatchObject({session_id:item.id,workflow:'diagnose',background:true});
    return route.fulfill({contentType:'text/event-stream',body:'data: {"type":"final","end_reason":"answered"}\n\ndata: {"type":"done"}\n\n'});
  });
  await page.goto('/websites/design-site');
  await page.getByRole('switch',{name:'启用定时 HTTP 巡检'}).click();
  await page.getByRole('button',{name:'查看巡检发现的问题'}).click();
  await expect(page).toHaveURL('/issues/design-case');
  await page.getByRole('button',{name:'分析巡检异常'}).click();
  await expect(page.locator('.support-process [role=status]')).toContainText('本次分析已完成');
});

test('390px 问题详情展示处理进度和去重后的复测对照',async({page})=>{
  const item: PersonalCase = {...diagnosis,status:'sample_recovered',plan:{revision:1,evidence_ids:['E1'],steps:diagnosis.report!.steps},
    sample_checks:{scope:'仅限用户提交样本',measurements:[{evidence_id:'E1',request_count:5,failed_requests:2,pool_error_lines:[],container_states:[],requests:[]}],
      comparison:{method:'GET',endpoint:'/orders',before_requests:2,before_failed_samples:2,before_from:now,before_until:now,
        after_requests:3,after_failures:0,after_duplicates:2,after_from:now,after_until:now},
      verification:{verdict:'sample_recovered',evidence_ids:['E1'],reason:'三个有效复测样本均成功'}}};
  await page.setViewportSize({width:390,height:844});await fixture(page,{item});await page.goto('/issues/design-case');
  const progress=page.getByRole('region',{name:'处理进度',exact:true});
  await expect(progress).toContainText('三个有效复测样本均成功');
  await expect(progress.getByRole('button',{name:'确认已解决',exact:true})).toBeInViewport();
  const comparison=page.getByRole('region',{name:'处理前后对照',exact:true});
  await expect(comparison).toContainText('3 个有效样本 · 0 个失败');
  await expect(comparison).toContainText('合并了 2 条重复记录');
  await progress.getByRole('button',{name:'确认已解决',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'由你确认问题已解决'})).toBeVisible();
  await noOverflow(page);
});

test('重新打开问题时订阅后台运行并读取已保存结果',async({page})=>{
  let completed=false, submitted=0;
  await fixture(page,{item:diagnosis});
  await page.route('**/api/agent/chat/stream',route=>{submitted++;return route.abort();});
  await page.route('**/api/sessions/design-case',route=>route.fulfill({json:{
    title:diagnosis.title,messages:history,active_run_id:completed?null:'design-run',
  }}));
  await page.route('**/api/agent/runs/design-run/events?after=0',route=>{
    completed=true;
    const events: AgentEvent[] = [{type:'connected',run_id:'design-run'},{type:'final',run_id:'design-run',end_reason:'answered'},{type:'done'}];
    return route.fulfill({contentType:'text/event-stream',body:events.map(event=>'data: '+JSON.stringify(event)+'\n\n').join('')});
  });
  await page.goto('/issues/design-case');
  await expect(page.locator('.support-process [role=status]')).toContainText('本次分析已完成');
  expect(submitted).toBe(0);
});

for(const width of [1440,1024,390]) {
  test(`${width}px 登录注册等高与隐私说明`,async({page},info)=>{
    await page.setViewportSize({width,height:900});await fixture(page,{signedIn:false});await page.goto('/home');
    await expect(page.getByRole('tab',{name:'注册',exact:true})).toBeVisible();
    const card=page.locator('.login-card'),login=await card.boundingBox();
    await page.screenshot({path:info.outputPath('login.png')});
    await page.getByRole('tab',{name:'注册',exact:true}).click();
    await expect(page.getByLabel('确认密码',{exact:true})).toBeVisible();
    await page.getByRole('button',{name:'注册说明',exact:true}).click();
    await expect(page.getByText(/每次注册创建独立私人空间，不会加入其他组织/)).toBeVisible();
    const registration=await card.boundingBox();expect(Math.abs(registration!.height-login!.height)).toBeLessThan(1);
    await page.screenshot({path:info.outputPath('register.png')});
    await page.getByRole('tab',{name:'登录',exact:true}).click();
    await expect(page.getByLabel('确认密码',{exact:true})).toHaveCount(0);
    if(width>700)await expect(page.locator('.login-brand')).toBeVisible();else await expect(page.locator('.login-story')).toBeHidden();await noOverflow(page);
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
    const assistant = page.getByRole('button',{name:'智能维护助手',exact:true});
    await expect(assistant).toHaveClass(/ant-btn-primary/);
    await expect(assistant).toHaveAttribute('aria-current','page');
    const assistantLabel = assistant.getByText('智能助手',{exact:true});
    if(width===390) await expect(assistantLabel).toBeHidden();
    else await expect(assistantLabel).toBeVisible();
    await expect(page.getByRole('heading',{name:'今天想聊点什么？'})).toBeVisible();
    await expect(page.getByLabel('给维护助手发消息')).toBeEnabled();
    await expect(page.getByLabel('选择网站')).toHaveCount(0);
    await expect(page.getByRole('button',{name:'诊断',exact:true})).toBeVisible();
    await noOverflow(page);await page.screenshot({path:info.outputPath('plain-assistant.png')});
    await page.goto('/support?mode=diagnose');
    await expect(page.getByRole('heading',{name:'网站遇到了什么问题？'})).toBeVisible();
    await expect(page.locator('.support-suggestions button')).toHaveCount(4);
    const composer=await page.locator('.support-composer').boundingBox();expect(composer!.y+composer!.height).toBeLessThanOrEqual(844);
    await expect(page.getByRole('button',{name:'补充证据',exact:true})).toHaveCount(1);
    await expect(page.getByLabel('网站排障问题')).toHaveAttribute('placeholder',/脱敏/);
    if(width===390){
      await expect(page.locator('.support-conversations')).toBeHidden();
      await expect(page.getByRole('button',{name:'排查历史',exact:true})).toBeVisible();
      await page.getByRole('button',{name:'打开功能导航'}).click();
      await expect(page.getByRole('menuitem',{name:'我的网站'})).toBeInViewport();
      await expect(page.getByRole('menuitem',{name:'审批与工单'})).toHaveCount(0);
      await page.keyboard.press('Escape');
      await expect(page.locator('.navigation-drawer')).toBeHidden();
    }
    await noOverflow(page);await page.screenshot({path:info.outputPath('assistant.png')});
  });
}

for(const {role,tenantId} of [
  {role:'viewer',tenantId:'design'}, {role:'engineer',tenantId:'design'},
  {role:'admin',tenantId:'design'}, {role:'admin',tenantId:'default'},
]) {
  test(`${role} (${tenantId}) 导航权限`,async({page})=>{
    await page.setViewportSize({width:1440,height:900});await fixture(page,{role,tenantId});await page.goto('/home');
    await expect(page.getByRole('menuitem',{name:'我的网站'})).toBeVisible();
    await expect(page.getByRole('button',{name:'智能维护助手',exact:true})).toHaveCount(1);
    await expect(page.getByRole('menuitem',{name:'智能维护助手'})).toHaveCount(0);
    await expect(page.getByRole('menuitem',{name:'审批与工单'})).toHaveCount(role==='viewer'?0:1);
    await expect(page.getByRole('menuitem',{name:'评测记录'})).toHaveCount(role==='admin'&&tenantId==='default'?1:0);
    await expect(page.getByText('管理与质量',{exact:true})).toHaveCount(role==='viewer'?0:1);
    await expect(page.getByText('服务已就绪',{exact:true})).toBeVisible();
    if(role==='admin'&&tenantId!=='default'){
      await page.goto('/evaluation');
      await expect(page.getByText(/需要平台管理员权限/)).toBeVisible();
    }
  });
}

for(const existing of [true,false]) {
  test(`${existing?'已有':'新建'}对话提交证据后保留标题`,async({page})=>{
    await fixture(page,{messages:[]});
    await page.route(`**/api/support/cases/${record.id}/evidence`,route=>route.fulfill({json:{saved:true,replacements:1}}));
    await page.goto(existing?'/support/'+record.id:'/support?mode=diagnose');
    if(existing)await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    else await page.getByLabel('网站排障问题').fill(record.title!);
    await page.getByRole('button',{name:'补充证据',exact:true}).click();
    await page.getByLabel('证据文本',{exact:true}).fill('password=fictional-test-secret');
    const submit=page.getByRole('button',{name:'提交并分析',exact:true});
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    await page.reload();await expect(page.locator('.support-chat-top h3')).toHaveText(record.title!);
    await page.getByRole('button',{name:'新排查',exact:true}).click();
    await expect(page.locator('.support-chat-top h3')).toHaveText('网站排查');
  });
}

for(const state of ['archived','user_resolved']) {
  test(`${state} 报告、时间线和输入禁用`,async({page},info)=>{
    const item:PersonalCase={...record,status:state==='archived'?'investigating':state,archived:state==='archived',report:{workflow:'diagnose',summary:'已确认上游连接失败，尚需核对原因。',observations:[],hypotheses:['上游进程可能退出'],missing_info:['上游同一时间的日志'],steps:[{location:'服务器',action:'docker logs app',normal:'保留日志',abnormal:'核对退出原因',risk:'只读',rollback:''}],references:[],verification:{verdict:'inconclusive',evidence_ids:[],reason:'证据仍不足'}}};
    await page.setViewportSize({width:390,height:844});await fixture(page,{item});await page.goto('/issues/design-case');
    await expect(page.getByLabel('网站排障问题')).toBeDisabled();await expect(page.getByRole('button',{name:'发送',exact:true})).toBeDisabled();
    await expect(page.locator('.support-report-flow')).toContainText('当前判断');
    await page.getByText('分析详情',{exact:true}).click();
    await expect(page.locator('.support-chat-scroll .ant-card')).toContainText(['当前判断','处理结果核对','维护时间线']);
    await expect(page.locator('.support-result-card')).toContainText('下一步');
    await expect(page.getByRole('button',{name:'检查步骤说明',exact:true})).toBeAttached();
    await expect(page.getByRole('button',{name:'确认检查方案',exact:true})).toBeDisabled();
    await page.getByText('当前判断',{exact:true}).scrollIntoViewIfNeeded();await noOverflow(page);
    await page.screenshot({path:info.outputPath('report.png')});
    await page.getByRole('button',{name:'证据与资料 · 0'}).click();
    await page.getByRole('button',{name:'资料依据说明',exact:true}).click();
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

for(const width of [1440,1024,390]) {
  test(`${width}px 网站详情四分区与真实计数`,async({page},info)=>{
    await page.setViewportSize({width,height:900});await page.emulateMedia({reducedMotion:'reduce'});await fixture(page);await page.goto('/websites/design-site');
    await expect(page.getByRole('heading',{name:site.name,exact:true})).toBeVisible();
    await expect(page.getByRole('tab')).toHaveCount(4);
    await expect(page.locator('.site-stat strong')).toHaveText(['4','3','1','1']);
    await expect(page.locator('.site-action-list > div')).toContainText(['结算接口返回 502','等待检查数据库连接']);
    await expect(page.locator('.site-action-list')).not.toContainText('连接池配置已修复');
    await expect(page.locator('.site-action-list')).not.toContainText('已归档但尚未确认解决');
    await expect(page.getByRole('button',{name:'分析这个网站',exact:true})).toBeInViewport();
    await page.getByRole('button',{name:'单次HTTP检查说明',exact:true}).click();
    await expect(page.getByText(/不代表持续监控或故障已解决/)).toBeVisible();
    await expect(page.getByRole('tooltip')).toBeInViewport();
    await page.mouse.move(0,0);
    await page.getByRole('button',{name:'分析这个网站',exact:true}).focus();
    await expect(page.getByRole('tooltip')).not.toBeVisible();
    await expect(page.locator('.ant-tooltip')).toHaveCount(0);
    await page.locator('.app-content').evaluate(node=>node.scrollTop=0);
    await noOverflow(page);await page.screenshot({path:info.outputPath('website-overview.png'),fullPage:true});
    await page.getByRole('tab',{name:'问题记录（4）',exact:true}).click();
    await expect(page.locator('.site-issues-table')).toContainText('已解决');
    await expect(page.locator('.site-issues-table')).toContainText('已归档');
    await noOverflow(page);await page.screenshot({path:info.outputPath('website-issues.png'),fullPage:true});
    await page.getByRole('tab',{name:'维护资料（1）',exact:true}).click();
    await expect(page.getByText('结算服务维护手册',{exact:true})).toBeVisible();
    await expect(page.getByText('12 个有效片段',{exact:true})).toBeVisible();
    await expect(page.getByRole('button',{name:'上传 / 管理我的手册'})).toBeVisible();await noOverflow(page);
    await page.getByRole('tab',{name:'维护历史',exact:true}).click();
    await expect(page.getByText('登记问题',{exact:true})).toBeVisible();await noOverflow(page);
    await page.getByRole('button',{name:'分析这个网站',exact:true}).click();
    await expect(page).toHaveURL(/\/support\?site=design-site&workflow=diagnose$/);
  });
}

test('网站详情标签支持刷新、前进后退与未知值回退',async({page})=>{
  await fixture(page);await page.goto('/websites/design-site?tab=manuals&from=bookmark');
  const manuals=page.getByRole('tab',{name:'维护资料（1）',exact:true});
  await expect(manuals).toHaveAttribute('aria-selected','true');
  await page.reload();await expect(manuals).toHaveAttribute('aria-selected','true');
  await page.getByRole('tab',{name:'维护历史',exact:true}).click();
  await expect(page).toHaveURL(/tab=history&from=bookmark$/);
  await page.goBack();await expect(manuals).toHaveAttribute('aria-selected','true');
  await page.goForward();await expect(page.getByRole('tab',{name:'维护历史',exact:true})).toHaveAttribute('aria-selected','true');
  await page.goto('/websites/design-site?tab=invalid');
  await expect(page.getByRole('tab',{name:'概况',exact:true})).toHaveAttribute('aria-selected','true');
});

test('网站详情加载失败可重试并保留标签',async({page})=>{
  await fixture(page);let calls=0;
  await page.route('**/api/support/sites/design-site',route=>route.fulfill(++calls===1?{status:503,json:{detail:'详情暂时不可用'}}:{json:siteDetail}));
  await page.goto('/websites/design-site?tab=manuals');
  await expect(page.getByText('网站详情加载失败',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'重试',exact:true}).click();
  await expect(page.getByRole('tab',{name:'维护资料（1）',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(page.getByText('结算服务维护手册',{exact:true})).toBeVisible();
  await expect(page).toHaveURL(/\/websites\/design-site\?tab=manuals$/);
});

test('网站详情迟到响应不能覆盖新网站或改变路由',async({page})=>{
  await fixture(page);
  await page.route('**/api/workspace/sites?*',route=>route.fulfill({json:{items:[{...site,id:'slow-site',name:'响应较慢的网站'},site],total:2,page:1,page_size:6}}));
  let release=()=>{};
  const gate=new Promise<void>(resolve=>{release=resolve;});
  await page.route('**/api/support/sites/slow-site',async route=>{await gate;await route.fulfill({json:{...siteDetail,id:'slow-site',name:'响应较慢的网站'}});});
  await page.goto('/websites');
  await page.locator('.home-cards .ant-card').filter({hasText:'响应较慢的网站'}).getByRole('button',{name:'打开网站',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('正在加载网站详情');
  await page.getByRole('navigation',{name:'面包屑'}).getByRole('link',{name:'我的网站',exact:true}).click();
  await page.locator('.home-cards .ant-card').filter({hasText:'个人网站'}).getByRole('button',{name:'打开网站',exact:true}).click();
  await expect(page.getByRole('heading',{name:'个人网站',exact:true})).toBeVisible();
  const response=page.waitForResponse('**/api/support/sites/slow-site');release();await response;
  // 后续标签操作同时确认旧请求结束后页面仍然可交互。
  await page.getByRole('tab',{name:'维护资料（1）',exact:true}).click();
  await expect(page).toHaveURL(/\/websites\/design-site\?tab=manuals$/);
  await expect(page.getByRole('heading',{name:'个人网站',exact:true})).toBeVisible();
});

test('网站详情深链接编辑和HTTP刷新保留当前位置',async({page})=>{
  await fixture(page);
  let savedName='';
  await page.route('**/api/support/sites/design-site',async route=>{
    if(route.request().method()==='PUT')savedName=route.request().postDataJSON().name;
    await route.fulfill({json:{...siteDetail,name:savedName || site.name}});
  });
  await page.route('**/api/maintenance/sites/design-site/http-check',route=>route.fulfill({json:{state:'normal'}}));
  await page.goto('/websites/design-site?tab=manuals');
  await page.getByRole('button',{name:'编辑网站',exact:true}).click();
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await expect(page.getByLabel('网站名称',{exact:true})).toHaveValue('个人网站');
  await page.getByLabel('网站名称',{exact:true}).fill('更新后的网站');
  await page.getByRole('button',{name:'保存网站',exact:true}).click();
  await expect(page.getByRole('heading',{name:'更新后的网站',exact:true})).toBeVisible();
  expect(savedName).toBe('更新后的网站');
  await expect(page.getByRole('tab',{name:'维护资料（1）',exact:true})).toHaveAttribute('aria-selected','true');
  await page.getByRole('tab',{name:'概况',exact:true}).click();
  await page.getByRole('button',{name:'主动检查HTTP响应',exact:true}).click();
  await expect(page.getByText('本次HTTP响应已记录',{exact:true})).toBeVisible();
  await expect(page).toHaveURL(/\/websites\/design-site\?tab=overview$/);
  await expect(page.getByRole('heading',{name:'更新后的网站',exact:true})).toBeVisible();
});

test('网站详情空记录、无公开网址和资料管理入口',async({page})=>{
  await fixture(page);
  await page.route('**/api/support/sites/design-site',route=>route.fulfill({json:{...siteDetail,url:'',documents:[],cases:[]}}));
  await page.goto('/websites/design-site');
  await expect(page.locator('.site-stat strong')).toHaveText(['0','0','0','0']);
  await expect(page.getByText('暂无需要处理的问题',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'主动检查HTTP响应',exact:true})).toBeDisabled();
  await page.getByRole('tab',{name:'问题记录（0）',exact:true}).click();
  await expect(page.getByText('这个网站还没有问题记录',{exact:true})).toBeVisible();
  await page.getByRole('tab',{name:'维护资料（0）',exact:true}).click();
  await expect(page.getByText('还没有关联手册',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'上传 / 管理我的手册'}).click();
  await expect(page).toHaveURL(/\/knowledge$/);
});

for(const width of [1440,1024,390]) {
  test(`${width}px 助手四区结果与日志原文定位`,async({page},info)=>{
    await page.setViewportSize({width,height:844});await page.emulateMedia({reducedMotion:'reduce'});
    await fixture(page,{item:diagnosis,messages:[history[0],{role:'assistant',content:diagnosis.report!.summary+'\n已观察到的证据与检查步骤'}]});
    await page.goto('/support/design-case');
    const result=page.locator('.support-result-card');
    await expect(result.getByRole('region')).toHaveCount(4);
    for(const label of ['发现什么','依据','下一步','还不确定'])await expect(result.getByRole('region',{name:label,exact:true})).toHaveCount(1);
    await expect(page.getByText(diagnosis.report!.summary,{exact:true})).toHaveCount(1);
    await expect(result).toContainText('连接可能没有及时释放');
    await expect(result).toContainText('同一时间的活跃连接数');
    await page.locator('.support-chat-scroll').evaluate(node=>{node.scrollTop=0;});
    await noOverflow(page);
    expect(await page.locator('.support-chat-scroll').evaluate(node=>node.scrollWidth<=node.clientWidth)).toBeTruthy();
    await page.screenshot({path:info.outputPath('assistant-result.png')});
    await result.getByRole('button',{name:'已提交的应用日志 · L45–L46',exact:true}).click();
    const drawer=page.locator('.ant-drawer-open');
    await expect(drawer.getByText('本次证据与资料',{exact:true})).toBeVisible();
    await expect(drawer.locator('.evidence-line.highlighted')).toHaveText(['L45: QueuePool limit reached','L46: application log line 46']);
    await expect(drawer.locator('.evidence-line.highlighted').first()).toBeFocused();
    await expect(drawer.locator('.evidence-line.highlighted').first()).toBeInViewport();
    expect(await drawer.locator('.evidence-lines').evaluate(node=>node.scrollTop)).toBeGreaterThan(0);
    await page.screenshot({path:info.outputPath('assistant-evidence.png')});
    await drawer.locator('.ant-drawer-close').click();
    await result.getByRole('button',{name:'[1] 官方资料 · 连接等待超时说明',exact:true}).click();
    await expect(page.locator('.ant-drawer-open').getByText('引用来源',{exact:true})).toBeVisible();
    await expect(page.locator('.ant-drawer-open .source-text').first()).toContainText('不代表本次根因');
    await noOverflow(page);
    await expect(page.getByLabel('选择网站',{exact:true})).toBeDisabled();
  });
}

for(const workflow of ['consult','history']) {
  test(`助手${workflow}简短回答保留来源且不套诊断步骤`,async({page})=>{
    const item:PersonalCase={...diagnosis,kind:workflow,report:{...diagnosis.report!,workflow,summary:'连接池可以复用数据库连接，减少重复建连。',observations:[],hypotheses:[],missing_info:[],steps:[]}};
    await fixture(page,{item,messages:[history[0],{role:'assistant',content:item.report!.summary+'\n\n资料引用：[1] 连接等待超时说明'}]});await page.goto('/support/design-case');
    await expect(page.locator('.support-report-flow')).toContainText(item.report!.summary);
    await expect(page.getByText(item.report!.summary,{exact:true})).toHaveCount(1);
    await expect(page.locator('.support-result-card')).toHaveCount(0);
    await expect(page.getByRole('button',{name:'确认检查方案',exact:true})).toHaveCount(0);
    await expect(page.getByText('处理结果核对',{exact:true})).toHaveCount(0);
    await page.getByRole('button',{name:'证据与资料 · 2',exact:true}).click();
    await expect(page.locator('.ant-drawer-open').getByText('[1] 连接等待超时说明',{exact:true})).toBeVisible();
  });
}

test('助手未提交日志时不提供虚构的行号依据',async({page})=>{
  await fixture(page,{item:{...diagnosis,evidence:[]}});await page.goto('/support/design-case');
  const result=page.locator('.support-result-card');
  await expect(result).toContainText('当前记录中没有可核对的已提交日志行');
  await expect(result.locator('.result-observation')).toHaveCount(0);
  await expect(result.getByRole('button',{name:/L45/})).toHaveCount(0);
  await expect(result).not.toContainText('QueuePool limit reached');
});

test('助手确认方案和步骤标记不自动改变恢复结论',async({page})=>{
  await fixture(page,{item:diagnosis});let item:PersonalCase=diagnosis;
  const statuses:string[]=[];
  await page.route('**/api/support/cases/design-case',route=>route.fulfill({json:item}));
  await page.route('**/api/support/cases/design-case/confirm-plan',route=>{
    item={...item,plan:{revision:1,evidence_ids:['E1'],steps:diagnosis.report!.steps,checks:[{status:'pending',note:''}]}};
    return route.fulfill({json:{message:'只记录确认，不会执行命令'}});
  });
  await page.route('**/api/support/cases/design-case/plan/steps/0',route=>{
    const body=route.request().postDataJSON();statuses.push(body.status);
    expect(body.revision).toBe(1);
    item={...item,plan:{...item.plan!,checks:[{status:body.status,note:body.note}]}};
    return route.fulfill({json:item});
  });
  await page.goto('/support/design-case');
  await page.locator('.result-steps .ant-collapse-header').click();
  await expect(page.locator('.result-steps')).toContainText('docker logs app --tail 100');
  await expect(page.locator('.result-steps')).toContainText('正常时：');
  await expect(page.locator('.result-steps')).toContainText('异常时：');
  await page.getByRole('button',{name:'确认检查方案',exact:true}).click();
  await expect(page.getByText('未检查',{exact:true})).toBeVisible();
  await page.getByLabel('检查1的结果记录').fill('已在本地核对，仍需提交新样本');
  await page.getByRole('button',{name:'记录已检查',exact:true}).click();
  await expect(page.getByText('用户已检查',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'记录暂未执行',exact:true}).click();
  await expect(page.getByText('暂未执行',{exact:true})).toBeVisible();
  expect(statuses).toEqual(['done','skipped']);expect(item.status).toBe('investigating');
  await expect(page.getByText('尚未提供操作后的新样本',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'提交此步的新证据',exact:true}).click();
  await expect(page.getByText('关联已确认方案的检查 1',{exact:true})).toBeVisible();
  await expect(page.getByText(/自动遮蔽常见密码/)).toBeVisible();
});

test('助手过程默认折叠且分析失败不会显示已完成',async({page})=>{
  await fixture(page);
  const events:AgentEvent[]=[{type:'phase',workflow:'diagnose',message:'正在核对网站资料',run_id:'design-run'},{type:'tool_start',name:'search_runbook'},{type:'tool',name:'search_runbook',summary:'没有找到适用资料',result:{internal:'不应展示的内部JSON'}},{type:'error',message:'本次模型请求失败'},{type:'final',end_reason:'model_error',answer:''}];
  await page.route('**/api/agent/chat/stream',route=>route.fulfill({contentType:'text/event-stream',body:events.map(event=>'data: '+JSON.stringify(event)+'\n\n').join('')}));
  await page.goto('/support/design-case');await page.getByLabel('网站排障问题').fill('请核对结算报错');await page.getByRole('button',{name:'发送',exact:true}).click();
  const process=page.locator('.support-process');
  await expect(process.locator('[role=status]')).toContainText('本次分析未完成，已有证据保留');
  await expect(page.getByRole('button',{name:'重试本次问题',exact:true})).toBeVisible();
  await expect(process.locator('.ant-collapse-header')).toHaveAttribute('aria-expanded','false');
  await process.locator('.ant-collapse-header').click();
  await expect(process).toContainText('检索适用资料');await expect(process).toContainText('本次模型请求失败');
  await expect(process).not.toContainText('本次分析已完成');await expect(process).not.toContainText('不应展示的内部JSON');
});

test('助手等待已有运行时可停止并保留记录',async({page})=>{
  await fixture(page);let cancelled=false;
  await page.route('**/api/agent/chat/stream',route=>route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'existing_run',run_id:'design-run',status:'running'})+'\n\ndata: {"type":"done"}\n\n'}));
  await page.route('**/api/agent/runs/design-run',route=>route.fulfill({json:{status:cancelled?'cancelled':'running'}}));
  await page.route('**/api/agent/runs/design-run/cancel',route=>{cancelled=true;return route.fulfill({json:{status:'cancel_requested'}});});
  await page.goto('/support/design-case');await page.getByLabel('网站排障问题').fill('请继续同一问题');await page.getByRole('button',{name:'发送',exact:true}).click();
  await expect(page.locator('.support-process [role=status]')).toContainText('已连接到相同请求，正在等待结果');
  await page.getByRole('button',{name:'停止生成',exact:true}).click();
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeVisible({timeout:6000});
  await expect(page.locator('.support-chat-scroll')).toContainText('先核对同一时间的上游日志。');
});

test('助手再次分析失败时旧报告始终保留原引用',async({page})=>{
  await fixture(page,{item:diagnosis});
  let analysisStarted=false,release=()=>{},received=()=>{};
  const gate=new Promise<void>(resolve=>{release=resolve;});
  const refreshRequested=new Promise<void>(resolve=>{received=resolve;});
  await page.route('**/api/support/cases/design-case',async route=>{
    if(analysisStarted){received();await gate;}
    await route.fulfill({json:diagnosis});
  });
  await page.route('**/api/agent/chat/stream',route=>{
    analysisStarted=true;
    const candidate={...diagnosis.report_hits![0],chunk_id:'new-candidate',doc_title:'新一轮候选资料'};
    return route.fulfill({contentType:'text/event-stream',body:[
      {type:'error',message:'本次报告生成失败，保留上次报告'},
      {type:'final',end_reason:'invalid_report',answer:'',hits:[candidate]},
      {type:'done'},
    ].map(event=>'data: '+JSON.stringify(event)+'\n\n').join('')});
  });
  await page.goto('/support/design-case');
  const result=page.locator('.support-result-card');
  await expect(result.getByRole('button',{name:'[1] 官方资料 · 连接等待超时说明',exact:true})).toBeVisible();
  await page.getByLabel('网站排障问题').fill('请结合新日志重新分析');
  await page.getByRole('button',{name:'发送',exact:true}).click();
  await refreshRequested;
  try {
    await expect(result.getByRole('button',{name:'[1] 官方资料 · 连接等待超时说明',exact:true})).toBeVisible();
    await expect(result).not.toContainText('新一轮候选资料');
  } finally {release();}
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeVisible();
  await expect(result.getByRole('button',{name:'[1] 官方资料 · 连接等待超时说明',exact:true})).toBeVisible();
});

const nextTodos:MaintenanceTodo[]=[
  {id:'ordinary',kind:'case',title:'普通问题',state:'继续排查',url:'/issues/design-case',priority:20,action:'continue_case',severity:'P2'},
  {id:'done',kind:'completed',title:'上次处理记录',state:'最近已完成',url:'/issues/design-case',priority:100,action:'view_run'},
  {id:'urgent',kind:'case',title:'紧急问题：结算接口异常',state:'待提交检查结果',url:'/issues/design-case',priority:0,action:'submit_result',severity:'P0'},
];
const logSummary:EvidenceSummaryData={total_lines:3,request_count:3,failed_requests:2,time_range:null,http_statuses:[{status:502,count:2,lines:[1,3]},{status:200,count:1,lines:[2]}],endpoints:[{method:'GET',endpoint:'/orders',count:3,failures:2,lines:[1,2,3]}],exceptions:[],repeated:[],scenarios:[],notice:'仅统计这份文本，不代表实时流量或根因判断。'};
for(const width of [1440,1024,390]){
  test(`${width}px 下一件事按优先级并排除完成记录`,async({page},info)=>{
    await page.setViewportSize({width,height:900});await fixture(page);
    await page.route('**/api/maintenance/todos',r=>r.fulfill({json:{items:nextTodos}}));
    await page.goto('/home');
    await expect(page.locator('.home-next')).toContainText('紧急问题：结算接口异常');
    await expect(page.locator('.home-todo-panel .home-section-heading .ant-tag')).toHaveText('2');
    await expect(page.locator('.home-todo')).toHaveCount(2);
    const shortcuts=await page.locator('.home-shortcuts button').evaluateAll(nodes=>nodes.map(node=>node.getBoundingClientRect().top));
    expect(shortcuts[0]).toBe(shortcuts[1]);if(width>700)expect(shortcuts[0]).toBe(shortcuts[3]);else expect(shortcuts[2]).toBeGreaterThan(shortcuts[0]);
    await expect(page.locator('.home-recent')).toContainText('上次处理记录');
    await expect(page.locator('.home-next .ant-btn')).toBeInViewport();await noOverflow(page);
    await page.screenshot({path:info.outputPath('home-next.png'),fullPage:true});
    await page.locator('.home-next').getByRole('button',{name:'提交检查结果',exact:true}).click();
    await expect(page).toHaveURL('/issues/design-case');
  });
  test(`${width}px 日志摘要只定位实际匹配行`,async({page},info)=>{
    const item:PersonalCase={...record,evidence:[{id:'E1',label:'HTTP片段',text:'GET /orders HTTP 502\nGET /orders HTTP 200\nGET /orders HTTP 502',kind:'initial',collected_at:now,submitted_at:now,summary:logSummary}]};
    await page.setViewportSize({width,height:900});await fixture(page,{item});await page.goto('/support/design-case');
    await page.getByRole('button',{name:'证据与资料 · 1'}).click();
    await page.getByRole('button',{name:/HTTP片段/}).click();
    await expect(page.getByLabel('日志关键信息')).toContainText('3 次HTTP请求，其中 2 次失败');
    await page.getByRole('button',{name:/HTTP 502 · 2 次/}).click();
    await expect(page.locator('.evidence-line.highlighted')).toHaveCount(2);
    await expect(page.locator('.evidence-line').nth(1)).not.toHaveClass(/highlighted/);
    await expect(page.locator('.evidence-line').first()).toBeFocused();await noOverflow(page);
    await page.screenshot({path:info.outputPath('log-summary.png'),fullPage:true});
  });
  test(`${width}px 演练默认摘要和中断问题预填`,async({page},info)=>{
    await page.setViewportSize({width,height:900});await fixture(page);
    await page.route('**/api/workspace/sessions?*',r=>r.fulfill({json:{items:[{id:'demo-case',title:'原演练问题',created_at:now}],total:1}}));
    await page.route('**/api/sessions/demo-case',r=>r.fulfill({json:{id:'demo-case',title:'原演练问题',messages:[{role:'user',content:'调查 order-service 错误'}]}}));
    await page.route('**/api/remediations/actions',r=>r.fulfill({json:{enabled:true,service:'orders-drill-service'}}));
    await page.route('**/api/agent/sessions/demo-case/pending-approval',r=>r.fulfill({json:{pending:false}}));
    await page.route('**/api/agent/runs/interrupted-run',r=>r.fulfill({json:{run_id:'interrupted-run',status:'interrupted',created_at:now,updated_at:'2026-11-13T04:00:08Z',data:{question:'调查 order-service 错误',result:{message:'服务重启，执行中断'},events:[{kind:'progress',detail:{type:'node_status',node:'retrieve',message:'已取得手册'}}]}}}));
    let submitted=0;page.on('request',r=>{if(r.url().includes('/chat/stream'))submitted++;});
    await page.goto('/investigation/demo-case?run=interrupted-run');
    await expect(page.getByLabel('本次调查摘要')).toContainText('执行被中断');
    await expect(page.getByLabel('本次调查摘要')).toContainText('8.0 秒');
    await expect(page.locator('.evidence-scroll pre:visible')).toHaveCount(0);
    await page.getByRole('button',{name:'用已保存问题重新调查'}).click();
    await expect(page.getByLabel('调查问题')).toHaveValue('调查 order-service 错误');expect(submitted).toBe(0);
    await expect(page.getByRole('button',{name:'开始调查',exact:true})).toBeEnabled();
    await expect(page).toHaveURL(/run=interrupted-run/);await noOverflow(page);
    await page.screenshot({path:info.outputPath('investigation-summary.png'),fullPage:true});
    await page.getByRole('button',{name:'查看调查原始数据',exact:true}).click();
    await expect(page.locator('.evidence-scroll pre:visible')).toContainText('question');
  });
}

test('首页待办重试入库成功后及时消失，普通用户无审批按钮',async({page})=>{
  await fixture(page);let items:MaintenanceTodo[]=[{id:'doc-fail',kind:'document',resource_id:'private-doc',title:'失败的维护手册',state:'资料入库失败，可重试',url:'/knowledge/private-doc',action:'retry_document',priority:15},{id:'forbidden',kind:'approval',title:'不该给普通用户的审批',state:'待审批',url:'/operations/forbidden',action:'review_approval',priority:0}];
  await page.route('**/api/maintenance/todos',r=>r.fulfill({json:{items}}));
  await page.route('**/api/maintenance/documents/retry-failed',async r=>{expect(r.request().postDataJSON()).toEqual({ids:['private-doc']});items=items.filter(item=>item.id!=='doc-fail');await r.fulfill({json:{items:[{id:'private-doc',status:'queued'}]}});});
  await page.goto('/home');await expect(page.locator('.home-next')).toContainText('失败的维护手册');
  await expect(page.getByRole('button',{name:'前往审批',exact:true})).toHaveCount(0);
  await page.locator('.home-next').getByRole('button',{name:'重试入库',exact:true}).click();
  await expect(page.locator('.home-next')).toContainText('当前没有待处理事项');
  await page.locator('.todo-trigger').click();await expect(page.locator('.todo-popover')).toContainText('暂无待办');
});

test('首页数据加载失败可重试，空态不伪造任务',async({page})=>{
  await fixture(page);let failed=true;
  await page.route('**/api/maintenance/todos',r=>r.fulfill(failed?{status:503,json:{detail:'临时不可用'}}:{json:{items:[]}}));
  await page.goto('/home');await expect(page.getByText('待办加载失败',{exact:true})).toBeVisible();
  await expect(page.locator('.home-next')).toContainText('待办暂时无法更新');
  failed=false;await page.locator('.home-todo-panel').getByRole('button',{name:'重试',exact:true}).click();
  await expect(page.locator('.home-next')).toContainText('当前没有待处理事项');
  await expect(page.getByText('待办加载失败',{exact:true})).toHaveCount(0);
});

test('场景与官方指南带上下文进入助手且不自动发送',async({page})=>{
  await fixture(page);let submitted=0;page.on('request',r=>{if(r.url().includes('/chat/stream'))submitted++;});
  await page.goto('/knowledge');await page.getByRole('button',{name:'按此场景提问',exact:true}).first().click();
  await expect(page).toHaveURL(/workflow=diagnose/);
  await expect(page.getByLabel('网站排障问题')).toHaveValue(/尚未提交自己网站的日志/);
  await page.goto('/knowledge?tab=official');await page.getByRole('button',{name:'询问这份指南',exact:true}).click();
  await expect(page.getByLabel('网站排障问题')).toHaveValue(/连接等待超时指南.*2.x/);expect(submitted).toBe(0);
});

test('已有问题的中断提问保留网站和证据且不自动发送',async({page})=>{
  await fixture(page,{item:diagnosis});let submitted=0;page.on('request',r=>{if(r.url().includes('/chat/stream'))submitted++;});
  await page.goto('/support/design-case?workflow=auto&question='+encodeURIComponent('继续用已保存证据排查'));
  await expect(page.getByLabel('网站排障问题')).toHaveValue('继续用已保存证据排查');
  await expect(page.getByRole('button',{name:'证据与资料 · 2'})).toBeVisible();
  await expect(page.getByLabel('选择网站')).toBeDisabled();expect(submitted).toBe(0);
});

test('脱敏类别与IP核对展示，且无需强制预览即可提交',async({page})=>{
  await fixture(page);await page.goto('/support/design-case');
  await page.route('**/api/support/evidence/preview',r=>r.fulfill({json:{text:'password=[已脱敏]\n10.0.0.8 GET /orders HTTP 502',replacements:1,findings:[{label:'口令或密钥',count:1,lines:[1]}],review_findings:[{label:'IPv4地址',count:1,lines:[2]}],summary:logSummary}}));
  await page.getByRole('button',{name:'补充证据',exact:true}).click();
  const input=page.getByLabel('证据文本',{exact:true});await input.fill('password=original-secret\n10.0.0.8 GET /orders HTTP 502');
  await expect(page.getByRole('button',{name:'提交并分析',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'脱敏预览',exact:true}).click();
  await expect(page.getByRole('dialog')).toContainText('口令或密钥 1 处');await expect(page.getByRole('dialog')).toContainText('IPv4地址 1 处');
  await input.fill('修改过的文本');await expect(page.getByRole('button',{name:'提交并分析',exact:true})).toBeEnabled();
});

test('演练工单默认字段摘要且保留审批权限',async({page})=>{
  await fixture(page);await page.route('**/api/workspace/sessions?*',r=>r.fulfill({json:{items:[],total:0}}));
  await page.route('**/api/remediations/actions',r=>r.fulfill({json:{enabled:false,service:'order-service'}}));
  await page.route('**/api/sessions/demo-approval',r=>r.fulfill({json:{id:'demo-approval',messages:[]}}));
  await page.route('**/api/agent/sessions/demo-approval/pending-approval',r=>r.fulfill({json:{pending:true,approval_id:'approval-one',ticket_draft:{title:'连接池故障工单',service:'order-service',severity:'P1',description:'核对连接释放'}}}));
  await page.goto('/investigation/demo-approval');await expect(page.locator('.approval-card')).toContainText('连接池故障工单');
  await expect(page.locator('.approval-card pre')).not.toBeVisible();await expect(page.getByRole('button',{name:'批准创建工单'})).toBeDisabled();
  await page.getByRole('button',{name:'查看工单原始数据'}).click();await expect(page.locator('.approval-card pre')).toContainText('severity');
});

test('从中断待办重新分析同一问题会创建新的请求',async({page})=>{
  await fixture(page);const ids:string[]=[];
  await page.route('**/api/agent/chat/stream',route=>{
    ids.push(route.request().postDataJSON().request_id);
    return route.fulfill({contentType:'text/event-stream',body:'data: '+JSON.stringify({type:'final',run_id:'design-run',end_reason:'model_error'})+'\n\ndata: {"type":"done"}\n\n'});
  });
  await page.route('**/api/maintenance/todos',route=>route.fulfill({json:{items:[{id:'interrupted',kind:'run',title:'原有问题',state:'运行曾被重启中断',action:'resume_analysis',url:'/support/design-case?workflow=auto&question='+encodeURIComponent('请继续分析原问题')}]}}));
  await page.goto('/support/design-case');await page.getByLabel('网站排障问题').fill('请继续分析原问题');await page.getByRole('button',{name:'发送',exact:true}).click();
  await expect(page.getByRole('button',{name:'重试本次问题',exact:true})).toBeVisible();
  await page.locator('.todo-trigger').click();await page.locator('.todo-popover').getByRole('button',{name:'重新分析',exact:true}).click();
  await expect(page.getByLabel('网站排障问题')).toHaveValue('请继续分析原问题');expect(ids).toHaveLength(1);
  await page.getByRole('button',{name:'发送',exact:true}).click();await expect.poll(()=>ids.length).toBe(2);expect(ids[0]).not.toBe(ids[1]);
});

test('阅读指南时在旁边提问保留原页面且不自动发送',async({page})=>{
  await fixture(page);let calls=0;page.on('request',request=>{if(request.url().includes('/api/agent/chat/stream'))calls++;});
  await page.goto('/knowledge?tab=official');await page.getByRole('button',{name:'阅读指南与来源',exact:true}).click();
  await page.getByRole('button',{name:'在旁边询问这份资料',exact:true}).click();
  await expect(page).toHaveURL('/knowledge?tab=official');
  await expect(page.locator('.side-assistant').getByLabel('网站排障问题')).toHaveValue(/连接等待超时指南.*2.x/);
  expect(calls).toBe(0);
});

for(const width of [1280,390])test(`v3 ${width}px 紧凑等高登录和可聚焦说明`,async({page},info)=>{
  await page.setViewportSize({width,height:844});await fixture(page,{signedIn:false});
  await page.route('**/auth/options',route=>route.fulfill({json:{registration_enabled:true,visitor:{username:'visitor',password:'public-example'}}}));
  await page.goto('/home');await expect(page.getByRole('button',{name:'访客直接进入',exact:true})).toBeVisible();
  await expect(page.locator('.auth-confirmation-space')).toHaveCount(0);await expect(page.locator('.visitor-login code')).toHaveCount(0);
  const card=page.locator('.login-card');const login=await card.boundingBox();expect(login!.height).toBeLessThan(450);
  await page.screenshot({path:info.outputPath('login-v3.png'),animations:'disabled'});
  await page.getByRole('tab',{name:'注册',exact:true}).click();const registration=await card.boundingBox();expect(Math.abs(registration!.height-login!.height)).toBeLessThanOrEqual(1);
  await page.getByRole('button',{name:'注册说明',exact:true}).focus();await expect(page.getByRole('tooltip')).toContainText('独立私人空间');
  await page.getByRole('button',{name:'注册说明',exact:true}).click();await page.getByLabel('用户名',{exact:true}).focus();
  await page.screenshot({path:info.outputPath('register-v3.png'),animations:'disabled'});
  await page.locator('.notice-banner').getByRole('button').click();await expect(page.locator('.notice-banner')).not.toBeVisible();await noOverflow(page);
});

test('v3 全局搜索键盘选择、资料来源与失败提示',async({page})=>{
  await fixture(page);await page.route('**/api/workspace/documents?*',route=>route.fulfill({json:{items:[],total:0,page:1,page_size:6}}));
  await page.goto('/home');await page.keyboard.press('Control+k');const search=page.getByRole('dialog',{name:'搜索网站、问题与资料'});
  await expect(search.getByRole('option',{name:/个人网站.*网站/})).toBeVisible();
  await search.getByLabel('全局搜索关键词').press('ArrowDown');await expect(search.getByRole('option').nth(1)).toHaveAttribute('aria-selected','true');
  await search.getByLabel('全局搜索关键词').press('ArrowUp');await search.getByLabel('全局搜索关键词').press('Enter');await expect(page).toHaveURL('/websites/design-site');
  await expect(search).not.toBeVisible();await page.getByRole('button',{name:'全局搜索',exact:true}).click();
  await search.getByLabel('全局搜索关键词').fill('连接等待');await search.getByRole('option',{name:/连接等待超时指南.*官方资料/}).click();
  await expect(page.locator('.ant-drawer-open')).toContainText('核对超时日志与实际连接使用情况。');await page.locator('.ant-drawer-open .ant-drawer-close').click();
  await page.route('**/api/workspace/sites?*',route=>route.fulfill({status:503,json:{detail:'暂时失败'}}));
  await page.getByRole('button',{name:'全局搜索',exact:true}).click();await expect(search).toContainText('部分搜索结果加载失败');
  await page.keyboard.press('Escape');await expect(search).not.toBeVisible();await expect(page.getByRole('button',{name:'全局搜索',exact:true})).toBeFocused();
});

test('v3 导航折叠持久、面包屑与小屏抽屉',async({page},info)=>{
  await fixture(page);await page.setViewportSize({width:1440,height:900});await page.goto('/websites/design-site');
  await page.getByRole('button',{name:'折叠导航',exact:true}).click();await expect(page.locator('.main-sidebar')).toHaveClass(/ant-layout-sider-collapsed/);
  await page.reload();await expect(page.locator('.main-sidebar')).toHaveClass(/ant-layout-sider-collapsed/);
  await page.getByRole('button',{name:'展开导航',exact:true}).click();await expect(page.locator('.main-sidebar')).not.toHaveClass(/ant-layout-sider-collapsed/);
  await expect(page.getByRole('navigation',{name:'面包屑'})).toContainText('个人网站');
  await page.getByRole('navigation',{name:'面包屑'}).getByRole('link',{name:'我的网站',exact:true}).click();await expect(page).toHaveURL('/websites');
  await page.setViewportSize({width:800,height:900});await expect(page.locator('.main-sidebar')).toHaveClass(/ant-layout-sider-collapsed/);
  await page.setViewportSize({width:390,height:844});await expect(page.locator('.main-sidebar')).toHaveCount(0);
  await page.getByRole('button',{name:'打开功能导航'}).click();await expect(page.getByRole('menuitem',{name:'我的网站'})).toBeVisible();
  await page.getByRole('menuitem',{name:'我的网站'}).click();await noOverflow(page);await page.screenshot({path:info.outputPath('navigation-v3.png'),animations:'disabled'});
});

test('v3 新手引导与快捷入口打开真实表单',async({page})=>{
  await fixture(page);await page.route('**/api/workspace/sites?*',route=>route.fulfill({json:{items:[],total:0,page:1,page_size:6}}));
  await page.goto('/home');await expect(page.getByLabel('开始使用')).toBeVisible();
  await page.locator('.home-shortcuts').getByRole('button',{name:'登记网站',exact:true}).click();await expect(page.getByRole('dialog',{name:'添加网站'})).toBeVisible();
  await page.getByRole('dialog',{name:'添加网站'}).getByRole('button',{name:'取消'}).click();
  await page.goto('/home');await page.getByRole('button',{name:'记录新问题',exact:true}).click();await expect(page.getByRole('dialog',{name:'创建网站问题记录'})).toBeVisible();
  await page.getByRole('dialog',{name:'创建网站问题记录'}).getByRole('button',{name:'取消'}).click();
  await page.route('**/api/workspace/documents?*',route=>route.fulfill({json:{items:[],total:0,page:1,page_size:10}}));
  await page.goto('/home');await page.locator('.home-shortcuts').getByRole('button',{name:'上传手册',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'上传运维文档'})).toBeVisible();await expect(page.getByRole('dialog',{name:'上传运维文档'})).toContainText('上传前请移除密码、令牌');
});

test('v3 访客搜索不出现写入或管理操作，空结果可退出',async({page})=>{
  await fixture(page,{guest:true});
  await page.route('**/api/workspace/documents?*',route=>route.fulfill({json:{items:[],total:0,page:1,page_size:6}}));
  await page.goto('/home');await expect(page.getByLabel('开始使用')).toHaveCount(0);
  await page.getByRole('button',{name:'全局搜索',exact:true}).click();const dialog=page.getByRole('dialog',{name:/搜索网站、问题与资料/});
  await expect(dialog.getByRole('option',{name:/个人网站/})).toBeVisible();
  await expect(dialog.getByRole('option',{name:/上传手册|记录新问题|审批与工单|评测记录/})).toHaveCount(0);
  await page.route('**/api/workspace/sites?*',route=>route.fulfill({json:{items:[],total:0,page:1,page_size:6}}));
  await page.route('**/api/workspace/cases?*',route=>route.fulfill({json:{items:[],total:0,page:1,page_size:6}}));
  await dialog.getByLabel('全局搜索关键词').fill('没有匹配的名字');await expect(dialog).toContainText('没有找到可访问的匹配记录');
  await page.keyboard.press('Escape');await expect(dialog).not.toBeVisible();
});

for(const width of [1440,1024,390])test(`${width}px 优化首页稀疏记录和统一右栏`,async({page},info)=>{
  await page.setViewportSize({width,height:900});await fixture(page);await page.goto('/home');
  await expect(page.locator('.home-next')).toContainText('当前没有待处理事项');
  await expect(page.locator('.home-next')).toBeInViewport();await expect(page.locator('.home-recent')).toHaveCount(0);
  await expect(page.getByRole('complementary',{name:'待办与动态'})).toContainText('暂无待办');
  if(width>1100){const box=await page.locator('.home-overview').boundingBox();expect(box!.width).toBeGreaterThanOrEqual(280);expect(box!.width).toBeLessThanOrEqual(340);}
  await noOverflow(page);await page.screenshot({path:info.outputPath('sparse-home.png'),animations:'disabled'});
});

for(const width of [900,390])test(`${width}px 优化顶栏身份和通知入口`,async({page},info)=>{
  await page.setViewportSize({width,height:844});await fixture(page);await page.goto('/home');
  await expect(page.locator('.app-header .role-tag')).toHaveCount(0);
  const todo=page.getByRole('button',{name:'待办 0',exact:true});await expect(todo).toBeVisible();await expect(todo.locator('.ant-badge')).toBeVisible();
  await todo.click();await expect(page.locator('.todo-popover')).toContainText('暂无待办');await todo.click();
  await expect(page.locator('.global-search-trigger > span:not(.ant-btn-icon)')).toBeHidden();
  await page.getByRole('button',{name:'账号：design_user',exact:true}).click();await expect(page.locator('.account-identity')).toContainText('普通用户');
  await page.keyboard.press('Escape');await noOverflow(page);await page.screenshot({path:info.outputPath('compact-header.png'),animations:'disabled'});
});

test('优化助手保留唯一主结论，详情按需展开和手机网站整行',async({page},info)=>{
  const summary='先核对同一请求的上游监听与应用日志，目前还不能确认根因。';
  const item:PersonalCase={...diagnosis,report:{...diagnosis.report!,workflow:'consult',summary}};
  await fixture(page,{item,messages:[history[0],{role:'assistant',content:summary}]});await page.setViewportSize({width:390,height:844});await page.goto('/support/design-case');
  await expect(page.getByText(summary,{exact:true})).toHaveCount(1);await expect(page.locator('.support-answer')).toBeVisible();
  await expect(page.locator('.maintenance-timeline')).not.toBeVisible();
  const select=await page.locator('.support-top-actions .ant-select').boundingBox(),tools=await page.locator('.support-top-actions').boundingBox(),evidence=await page.locator('.support-evidence-trigger').boundingBox();
  expect(select!.width).toBeGreaterThan(tools!.width-2);expect(evidence!.y).toBeGreaterThanOrEqual(select!.y+select!.height);
  await page.screenshot({path:info.outputPath('focused-answer.png'),animations:'disabled'});
  await page.getByText('分析详情',{exact:true}).click();await expect(page.locator('.maintenance-timeline')).toContainText('登记问题');
  await page.getByText('分析详情',{exact:true}).click();await expect(page.locator('.maintenance-timeline')).not.toBeVisible();await noOverflow(page);
});

test('关闭演练后隐藏入口并将旧链接带回工作台',async({page})=>{
  await fixture(page,{role:'admin',tenantId:'default'});
  await page.route('**/auth/options',route=>route.fulfill({json:{drill_enabled:false}}));
  await page.route('**/api/workspace/tickets?*',route=>route.fulfill({json:{items:[],total:0}}));
  const repairRequests:string[]=[];
  page.on('request',request=>{if(request.url().includes('/api/workspace/remediations'))repairRequests.push(request.url());});
  await page.goto('/investigation/old-session');
  await expect(page).toHaveURL(/\/home$/);
  await expect(page.getByText('故障演练',{exact:true})).toHaveCount(0);
  await page.keyboard.press('Control+k');
  await page.getByLabel('全局搜索关键词').fill('故障演练');
  await expect(page.getByRole('option',{name:/个人网站/})).toBeVisible();
  await expect(page.getByRole('option',{name:/故障演练/})).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.goto('/operations');
  await expect(page.getByText(/待审批工单 ·/)).toBeVisible();
  await expect(page.getByText(/演练修复 ·/)).toHaveCount(0);
  expect(repairRequests).toEqual([]);
});

test('演练调查示例使用当前配置的服务名',async({page})=>{
  await fixture(page,{role:'admin',tenantId:'default'});
  await page.route('**/api/workspace/sessions?*',r=>r.fulfill({json:{items:[],total:0}}));
  await page.route('**/api/remediations/actions',r=>r.fulfill({json:{enabled:false,service:'orders-drill-service'}}));
  await page.goto('/investigation');
  await page.getByRole('button',{name:'填入调查示例'}).click();
  await expect(page.getByLabel('调查问题')).toHaveValue(/orders-drill-service/);
});


test('停止演练调查即时反馈并请求当前运行',async({page})=>{
  await fixture(page,{role:'admin'});
  await page.route('**/api/workspace/sessions?*',r=>r.fulfill({json:{items:[],total:0}}));
  await page.route('**/api/sessions',r=>r.fulfill({json:{id:'t78-session',title:'演练调查'}}));
  await page.route('**/api/remediations/actions',r=>r.fulfill({json:{enabled:false,service:'order-service'}}));
  const cancel=page.waitForRequest(r=>r.url().endsWith('/api/agent/runs/design-run/cancel'));
  await page.addInitScript(()=>{
    const original=window.fetch;
    window.fetch=async(input,init)=>{
      if(String(input)!=='/api/agent/chat/stream')return original(input,init);
      return new Response(new ReadableStream({start(controller){
        const event={type:'connected',run_id:'design-run'};
        controller.enqueue(new TextEncoder().encode('data: '+JSON.stringify(event)+'\n\n'));
        init?.signal?.addEventListener('abort',()=>controller.close(),{once:true});
      }}),{headers:{'Content-Type':'text/event-stream'}});
    };
  });
  await page.goto('/investigation');
  await page.getByLabel('调查问题').fill('检查演练连接池');
  await page.getByRole('button',{name:'开始调查'}).click();
  await page.getByRole('button',{name:'停止调查'}).click();
  await expect(page.getByText('已请求停止',{exact:true})).toBeVisible({timeout:2000});
  expect((await cancel).method()).toBe('POST');
});
