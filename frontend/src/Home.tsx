import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Empty, Skeleton, Tag } from 'antd';
import { ArrowRightOutlined, GlobalOutlined, PlusOutlined } from '@ant-design/icons';
import { api, formatTime } from './api';
import type { PageData, PersonalCase, Site } from './types';
import { caseStates } from './supportTypes';

type Todo = {id:string;title:string;state:string;url:string};
const statusColor = (status:string) => status === 'user_resolved' ? 'success' : status === 'awaiting_results' ? 'processing' : 'warning';

export default function Home({navigate,canWrite}: {navigate:(path:string)=>void;canWrite:boolean}) {
  const [cases,setCases]=useState<PageData<PersonalCase>|null>(null),[sites,setSites]=useState<PageData<Site>|null>(null);
  const [todos,setTodos]=useState<Todo[]>([]),[approvals,setApprovals]=useState(0),[loading,setLoading]=useState(true),[error,setError]=useState('');
  const refresh=useCallback(async()=>{
    setLoading(true);setError('');
    try{
      const [caseData,siteData,todoData,approvalData]=await Promise.all([
        api<PageData<PersonalCase>>('/api/workspace/cases?case_kind=problem&archived=false&page=1&page_size=6'),
        api<PageData<Site>>('/api/workspace/sites?page=1&page_size=6'),
        api<{items:Todo[]}>('/api/maintenance/todos'),
        canWrite?api<PageData<Record<string,unknown>>>('/api/workspace/approvals?status=pending&page=1&page_size=1'):Promise.resolve(null),
      ]);
      setCases(caseData);setSites(siteData);setTodos(todoData.items);setApprovals(approvalData?.total||0);
    }catch(e){setError((e as Error).message);}finally{setLoading(false);}
  },[canWrite]);
  useEffect(()=>{void refresh();},[refresh]);
  const activeTodos=todos.filter(item=>item.state!=='最近已完成');
  const firstUse=!loading&&!cases?.total&&!sites?.total;
  return <div className="page-stack home-page">
    <div className="page-title"><div><h2>维护工作台</h2><p>先处理需要你确认的事，再继续上次排查。</p></div><Button type="primary" icon={<PlusOutlined />} onClick={()=>navigate('/support')}>发起维护咨询</Button></div>
    {error&&<Alert type="error" title="工作台数据加载失败" description={error} action={<Button onClick={()=>void refresh()}>重新加载</Button>}/>}
    <section className="home-stats" aria-label="维护工作概览">
      <button type="button" className="stat-card" onClick={()=>navigate('/websites')}><span className="stat-icon"><GlobalOutlined /></span><span><small>已记录网站</small><strong>{sites?.total??'—'}</strong><em>查看环境与最近检查</em></span></button>
      <button type="button" className="stat-card" onClick={()=>navigate('/issues')}><span><small>未归档问题</small><strong>{cases?.total??'—'}</strong><em>继续排查或确认结果</em></span></button>
      <button type="button" className="stat-card" onClick={()=>document.getElementById('home-todos')?.scrollIntoView({behavior:'smooth'})}><span><small>当前待办</small><strong>{activeTodos.length}</strong><em>证据、结果与入库任务</em></span></button>
      {canWrite&&<button type="button" className="stat-card" onClick={()=>navigate('/operations')}><span><small>待审批</small><strong>{approvals}</strong><em>只涉及独立演练环境</em></span></button>}
    </section>
    {firstUse?<Card className="onboarding-panel"><div><span className="eyebrow">第一次使用</span><h3>从一次真实问题开始</h3><p>可以先记录网站环境，也可以直接描述异常。助手只提供指导，网站上的操作始终由你执行。</p><div className="onboarding-actions"><Button type="primary" onClick={()=>navigate('/support')}>直接描述问题</Button><Button onClick={()=>navigate('/websites')}>先添加网站</Button></div></div><ol className="getting-started"><li><span>1</span><div><strong>描述现象</strong><p>填写异常、影响和近期变更。</p></div></li><li><span>2</span><div><strong>提交证据</strong><p>上传脱敏日志，按建议逐项检查。</p></div></li><li><span>3</span><div><strong>确认结果</strong><p>核对提交样本，再记录实际结局。</p></div></li></ol></Card>:
    <div className="home-dashboard">
      <Card title="最近问题" extra={<Button type="link" onClick={()=>navigate('/issues')}>全部问题<ArrowRightOutlined /></Button>} className="workbench-panel">
        {loading&&!cases?<Skeleton active paragraph={{rows:5}}/>:!cases?.items.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有未归档问题"><Button type="primary" onClick={()=>navigate('/support?workflow=diagnose')}>开始第一次排查</Button></Empty>:
        <div className="work-list">{cases.items.map(item=><button className="work-item" key={item.id} onClick={()=>navigate('/issues/'+item.id)}><span className="work-item-main"><span className="work-item-title">{item.title||'未命名问题'}</span><small>{item.site_name||'未关联网站'} · 更新于 {formatTime(item.updated_at||item.created_at)}</small></span><span className="work-item-meta"><Tag color={statusColor(item.status)}>{caseStates[item.status]||item.status}</Tag><ArrowRightOutlined /></span></button>)}</div>}
      </Card>
      <Card id="home-todos" title="需要跟进" extra={<Tag>{activeTodos.length} 项</Tag>} className="workbench-panel">
        {!activeTodos.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待办，可以继续维护网站或咨询助手"/>:
        <div className="work-list">{activeTodos.slice(0,6).map(item=><button className="work-item" key={item.id} onClick={()=>navigate(item.url)}><span className="work-item-main"><span className="work-item-title">{item.title}</span><small>{item.state}</small></span><ArrowRightOutlined /></button>)}</div>}
      </Card>
    </div>}
    <section className="home-shortcuts" aria-label="常用功能"><button onClick={()=>navigate('/websites')}><strong>网站与检查</strong><span>维护环境，执行只读 HTTP 检查</span><ArrowRightOutlined /></button><button onClick={()=>navigate('/knowledge')}><strong>维护资料库</strong><span>管理手册，验证检索结果</span><ArrowRightOutlined /></button><button onClick={()=>navigate('/investigation')}><strong>独立故障演练</strong><span>体验调查、审批和恢复验证</span><ArrowRightOutlined /></button></section>
  </div>;
}
