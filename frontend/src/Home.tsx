import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Skeleton, Table, Tag } from 'antd';
import { ArrowRightOutlined, BookOutlined, CloseOutlined, ExperimentOutlined, GlobalOutlined, PlusOutlined, UnorderedListOutlined, UploadOutlined } from '@ant-design/icons';
import { savedAuth, api, formatTime } from './api';
import type { MaintenanceTodo, PageData, PersonalCase, Site } from './types';
import StatusTag, { SeverityTag } from './StatusTag';
import TodoAction, { isActiveTodo } from './TodoAction';
import InfoTip from './InfoTip';

type Activity={id:string|number;kind:string;at:string;case_id?:string;detail:{title?:string;label?:string};siteId:string;siteName:string};
type SkillItem={name:string;label:string};
type Section = 'cases'|'sites'|'todos'|'approvals'|'activity';
const activityLabels: Record<string,string> = {site_change:'网站变更',evidence_observed:'样本时间',http_incident:'巡检异常',http_probe_normal:'巡检响应正常',webhook_alert:'Webhook 告警'};

export default function Home({navigate,canWrite,drillEnabled}: {navigate:(path:string)=>void;canWrite:boolean;drillEnabled:boolean}) {
  const [cases,setCases]=useState<PageData<PersonalCase>|null>(null),[sites,setSites]=useState<PageData<Site>|null>(null);
  const [todos,setTodos]=useState<MaintenanceTodo[]|null>(null),[approvals,setApprovals]=useState<number|null>(null);
  const [activity,setActivity]=useState<Activity[]>([]),[onboarding,setOnboarding]=useState(true);
  const [skills,setSkills]=useState<SkillItem[]>([]);
  const [errors,setErrors]=useState<Partial<Record<Section,string>>>({});
  const versions=useRef<Partial<Record<Section,number>>>({});
  const guest=!!savedAuth()?.is_guest;
  const refresh=useCallback(async(section?:Section)=>{
    const requests: Record<Section, () => Promise<() => void>> = {
      cases: async()=>{const data=await api<PageData<PersonalCase>>('/api/workspace/cases?case_kind=problem&archived=false&page=1&page_size=6');return ()=>setCases(data);},
      sites: async()=>{const data=await api<PageData<Site>>('/api/workspace/sites?page=1&page_size=6');return ()=>setSites(data);},
      todos: async()=>{const data=await api<{items:MaintenanceTodo[]}>('/api/maintenance/todos');return ()=>setTodos(data.items);},
      approvals: async()=>{const data=await api<PageData<Record<string,unknown>>>('/api/workspace/approvals?status=pending&page=1&page_size=1');return ()=>setApprovals(data.total);},
      activity: async()=>{const data=await api<{events:Activity[]}>('/api/maintenance/activity');return ()=>setActivity(data.events);},
    };
    const keys:Section[]=section?[section]:['cases','sites','todos','activity',...(canWrite?['approvals' as const]:[])];
    await Promise.all(keys.map(async key=>{
      const version=(versions.current[key] || 0)+1;
      versions.current[key]=version;
      try {
        const apply=await requests[key]();
        if(versions.current[key]!==version)return;
        apply();
        setErrors(previous=>({...previous,[key]:undefined}));
      } catch(error) {
        if(versions.current[key]===version)setErrors(previous=>({...previous,[key]:(error as Error).message}));
      }
    }));
  },[canWrite]);
  useEffect(()=>{
    const load=()=>void refresh();load();
    api<SkillItem[]>('/api/agent/skills').then(list=>{if(Array.isArray(list))setSkills(list);}).catch(()=>{});
    window.addEventListener('opspilot:workspace-changed',load);window.addEventListener('focus',load);
    return()=>{window.removeEventListener('opspilot:workspace-changed',load);window.removeEventListener('focus',load);};
  },[refresh]);
  const errorNotice=(section:Section,label:string)=>errors[section]&&
    <Alert type="warning" title={label+'加载失败'} action={<Button size="small" onClick={()=>void refresh(section)}>重试</Button>}/>;
  const activeTodos=(todos || []).filter(isActiveTodo).filter(item=>item.kind!=='approval'||canWrite).sort((a,b)=>(a.priority??50)-(b.priority??50));
  const next=activeTodos[0],recent=(todos || []).filter(item=>!isActiveTodo(item));
  const siteItems = sites?.items || [];
  const webhookCount = siteItems.filter(s=>s.webhook?.enabled).length;
  const highPriorityCases = (cases?.items || []).filter(c=>c.severity==='P0'||c.severity==='P1').length;
  const quick=guest?[['浏览网站','/websites',<GlobalOutlined/>],['查看示例问题','/issues',<UnorderedListOutlined/>],['维护资料库','/knowledge',<BookOutlined/>]]:[['登记网站','/websites?create=1',<GlobalOutlined/>],['上传手册','/knowledge?tab=private&upload=1',<BookOutlined/>],['提交日志','/support?workflow=diagnose',<UploadOutlined/>]];
  if(drillEnabled)quick.push(['故障演练','/investigation',<ExperimentOutlined/>]);
  return <div className="page-stack home-page">
    <div className="page-title"><h2>维护工作台</h2><Button aria-label={guest?'浏览示例问题':'记录新问题'} type="primary" icon={guest?<ArrowRightOutlined aria-hidden/>:<PlusOutlined aria-hidden/>} onClick={()=>navigate(guest?'/issues':'/issues?create=1')}>{guest?'浏览示例问题':'记录新问题'}</Button></div>
    {errorNotice('sites','网站')}
    <section className="home-kpi-grid" aria-label="态势概览">
      <button type="button" className="home-kpi-card" onClick={()=>navigate('/websites')}>
        <div className="home-kpi-top"><span>纳管网站</span>{webhookCount>0&&<Tag color="cyan">Webhook {webhookCount}</Tag>}</div>
        <strong className="home-kpi-val">{sites?sites.total:'—'}</strong>
      </button>
      <button type="button" className="home-kpi-card kpi-alert" onClick={()=>navigate('/issues')}>
        <div className="home-kpi-top"><span>未归档问题</span>{highPriorityCases>0&&<Tag color="red">高优 {highPriorityCases}</Tag>}</div>
        <strong className="home-kpi-val">{cases?cases.total:'—'}</strong>
      </button>
      <button type="button" className="home-kpi-card kpi-warn" onClick={()=>navigate(canWrite&&approvals?'/operations':'/issues')}>
        <div className="home-kpi-top"><span>待办事项</span>{canWrite&&approvals!==null&&approvals>0&&<Tag color="gold">待审批 {approvals}</Tag>}</div>
        <strong className="home-kpi-val">{todos?activeTodos.length:'—'}</strong>
      </button>
      <button type="button" className="home-kpi-card" onClick={()=>navigate('/knowledge')}>
        <div className="home-kpi-top"><span>近期动态</span>{activity.length>0&&<Tag>{activityLabels[activity[0].kind]||'更新'}</Tag>}</div>
        <strong className="home-kpi-val">{activity.length}</strong>
      </button>
    </section>
    <section className={`home-next${next?'':' idle'}`} aria-label="下一件事">
      <div className="home-next-copy"><h3>接下来，先处理这一件</h3><strong className="home-next-title">{next?next.title:errors.todos?'待办暂时无法更新':!todos?'正在整理待办':'当前没有待处理事项'}</strong>
        {next&&<div className="home-next-meta"><Tag color="processing">{next.state}</Tag>{next.severity&&<SeverityTag value={next.severity}/>}</div>}
      </div><div className="home-next-action">{next?<TodoAction primary item={next} canWrite={canWrite} navigate={navigate} changed={()=>{setTodos(previous=>previous?.filter(item=>item.id!==next.id) || []);void refresh('todos');}}/>:<Button onClick={()=>navigate('/websites')}>管理我的网站</Button>}</div>
    </section>
    {skills.length>0&&<section className="home-skill-strip" aria-label="声明式 Skill">
      <strong>声明式 Skill</strong>
      <div className="home-skill-pills">{skills.map(item=><span className="home-skill-pill" key={item.name}><code>{item.name}</code>{item.label}</span>)}</div>
    </section>}
    <div className="home-shortcuts" aria-label="快捷入口">{quick.map(([label,url,icon])=><Button key={String(label)} aria-label={String(label)} icon={icon} onClick={()=>navigate(String(url))}>{label}</Button>)}</div>
    {!guest&&sites?.total===0&&!errors.sites&&onboarding&&<section className="onboarding" aria-label="开始使用"><strong>开始使用</strong><Button type="link" onClick={()=>navigate('/websites?create=1')}>1 登记网站</Button><Button type="link" onClick={()=>navigate('/knowledge?tab=private&upload=1')}>2 上传手册（选填）</Button><Button type="link" onClick={()=>navigate('/support?workflow=diagnose')}>3 提交第一份日志</Button><Button type="text" aria-label="收起引导" icon={<CloseOutlined/>} onClick={()=>setOnboarding(false)}/></section>}
    <div className="home-ledger-grid">
      <section className="home-ledger" aria-label="未归档问题"><div className="home-section-heading"><h3>未归档问题 <Tag>{cases?.total??'—'}</Tag></h3><Button type="link" onClick={()=>navigate('/issues')}>全部问题<ArrowRightOutlined/></Button></div>
        {errorNotice('cases','问题')}
        {!cases&&!errors.cases?<Skeleton active paragraph={{rows:5}}/>:cases?<Table<PersonalCase> rowKey="id" pagination={false} dataSource={cases?.items||[]} scroll={cases?.items?.length?{x:620}:undefined} locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有未归档问题"><Button type="primary" onClick={()=>navigate('/support?workflow=diagnose')}>开始第一次排查</Button></Empty>}} columns={[
          {title:'问题',key:'title',width:230,render:(_,item)=><div className="ledger-problem"><button onClick={()=>navigate('/issues/'+item.id)}>{item.title||'未命名问题'}</button>{item.problem?.endpoint&&<span className="mono muted">{item.problem.endpoint}</span>}</div>},
          {title:'网站',key:'site',width:110,responsive:['lg'],render:(_,item)=>item.site_name||'未关联网站'},
          {title:'严重度',key:'severity',width:85,render:(_,item)=><SeverityTag value={item.severity}/>},
          {title:<span>状态 <InfoTip label="问题状态说明">检查完成、样本改善、用户确认解决和归档分别记录；网站运行状态另以证据核对。</InfoTip></span>,key:'status',width:170,render:(_,item)=><StatusTag status={item.status}/>},
          {title:'更新于',key:'updated',width:165,responsive:['xl'],render:(_,item)=><span className="mono">{formatTime(item.updated_at||item.created_at)}</span>},
        ]}/>:null}
      </section>
      <aside className="home-overview" aria-label="待办与动态">
        <section className="home-todo-panel"><div className="home-section-heading"><h3>待办</h3><Tag>{todos?activeTodos.length:'—'}</Tag></div>
          {errorNotice('todos','待办')}
          {activeTodos.length?activeTodos.slice(0,4).map(item=><div className="home-todo" key={item.id}><div><strong>{item.title}</strong><Tag>{item.state}</Tag></div><TodoAction item={item} canWrite={canWrite} navigate={navigate} changed={()=>void refresh('todos')}/></div>)
            :todos&&<p className="home-todo-empty">暂无待办</p>}
          {canWrite&&<>{errorNotice('approvals','审批')}{approvals!==null&&<Button type="link" onClick={()=>navigate('/operations')}>待审批 {approvals}</Button>}</>}
        </section>
        {siteItems.length>0&&<section className="home-sites-panel"><div className="home-section-heading"><h3>纳管网站</h3><Button type="link" size="small" onClick={()=>navigate('/websites')}>管理</Button></div>
          {siteItems.slice(0,3).map(s=><button key={s.id} className="home-site-row" onClick={()=>navigate('/websites/'+s.id)}>
            <span className="home-site-name"><i className={`site-pulse ${s.health_check?.state==='abnormal'?'err':''}`} aria-hidden="true"/>{s.name}</span>
            <span className="mono home-site-meta">{s.health_check?.http_status?`HTTP ${s.health_check.http_status}`:`${s.open_count||0} 待处理`}</span>
          </button>)}
        </section>}
        {(activity.length>0||errors.activity)&&<section className="home-activity"><h3>最近动态 <InfoTip label="最近动态范围说明">按发生时间汇总当前账户所有网站的变更、巡检和样本时间。</InfoTip></h3>
          {errorNotice('activity','最近动态')}
          {activity.map(item=><button className="home-activity-item" key={item.siteId+':'+item.kind+':'+item.id} onClick={()=>navigate(item.case_id&&item.kind==='http_incident'?'/issues/'+item.case_id:'/websites/'+item.siteId+'?tab=history')}>
            <strong>{item.detail.title||item.detail.label||activityLabels[item.kind]}</strong><span>{formatTime(item.at)} · {activityLabels[item.kind]}</span><span>{item.siteName}</span>
          </button>)}
        </section>}
        {!!recent.length&&<section className="home-recent"><h3>最近完成</h3>{recent.slice(0,3).map(item=><button key={item.id} onClick={()=>navigate(item.url)}><Tag color="success">已完成</Tag><strong>{item.title}</strong></button>)}</section>}
      </aside>
    </div>
  </div>;
}
