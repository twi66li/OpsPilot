import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, Skeleton, Table, Tag } from 'antd';
import { ArrowRightOutlined, PlusOutlined } from '@ant-design/icons';
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
  return <div className="page-stack home-page">
    <div className="page-title"><h2>维护工作台</h2><Button type="primary" icon={<PlusOutlined/>} onClick={()=>navigate('/support')}>发起维护咨询</Button></div>
    {error&&<Alert type="error" title="工作台数据加载失败" description={error} action={<Button onClick={()=>void refresh()}>重新加载</Button>}/>}
    <div className="home-ledger-grid">
      <section className="home-ledger" aria-label="未归档问题"><div className="home-section-heading"><h3>未归档问题</h3><span><Tag color={activeTodos.length?'warning':undefined}>{activeTodos.length} 项待办</Tag><Button type="link" onClick={()=>navigate('/issues')}>全部问题<ArrowRightOutlined/></Button></span></div>
        {loading&&!cases?<Skeleton active paragraph={{rows:5}}/>:<Table<PersonalCase> rowKey="id" pagination={false} dataSource={cases?.items||[]} scroll={{x:720}} locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有未归档问题"><Button type="primary" onClick={()=>navigate('/support?workflow=diagnose')}>开始第一次排查</Button></Empty>}} columns={[
          {title:'问题',key:'title',width:250,render:(_,item)=><div className="ledger-problem"><button onClick={()=>navigate('/issues/'+item.id)}>{item.title||'未命名问题'}</button>{item.problem?.endpoint&&<span className="mono muted">{item.problem.endpoint}</span>}</div>},
          {title:'网站',key:'site',width:120,render:(_,item)=>item.site_name||'未关联网站'},
          {title:'严重度',key:'severity',width:85,render:(_,item)=><Tag color={item.severity==='P0'?'error':item.severity==='P1'?'warning':undefined}>{item.severity||'P2'}</Tag>},
          {title:'状态',key:'status',width:150,render:(_,item)=><Tag color={statusColor(item.status)}>{caseStates[item.status]||item.status}</Tag>},
          {title:'更新于',key:'updated',width:170,render:(_,item)=><span className="muted mono">{formatTime(item.updated_at||item.created_at)}</span>},
        ]}/>}
      </section>
      <aside className="home-overview" aria-label="概况与入口"><h3>概况</h3><div className="home-tally">
        <button onClick={()=>navigate('/websites')}><span>已记录网站</span><strong>{sites?.total??'—'}</strong></button>
        <button onClick={()=>navigate('/issues')}><span>未归档问题</span><strong>{cases?.total??'—'}</strong></button>
        <button onClick={()=>document.querySelector<HTMLButtonElement>('.app-header .todo-trigger')?.click()}><span>当前待办</span><strong className={activeTodos.length?'has-pending':''}>{activeTodos.length}</strong></button>
        <button disabled={!canWrite} onClick={()=>navigate('/operations')}><span>待审批</span><strong className={approvals?'has-pending':''}>{canWrite?approvals:'—'}</strong></button>
      </div><h3>常用入口</h3><div className="home-quick">{[['网站与检查','websites'],['维护资料库','knowledge'],['独立故障演练','investigation']].map(([label,path])=><button key={path} onClick={()=>navigate('/'+path)}><strong>{label}</strong><ArrowRightOutlined/></button>)}</div></aside>
    </div>
  </div>;
}
