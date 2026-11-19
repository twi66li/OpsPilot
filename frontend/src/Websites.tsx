import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Descriptions, Empty, Modal, Pagination, Input, Select, Space, Spin, Switch, Table, Tabs, Tag } from 'antd';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { savedAuth, api, copyText, post, formatTime } from './api';
import type { HttpMonitor, KnowledgeDoc, PageData, PersonalCase, Site, SupportEntry } from './types';
import StatusTag, { SeverityTag } from './StatusTag';
import SiteEditor from './SiteEditor';
import MaintenanceTimeline from './MaintenanceTimeline';
import SiteDependencies, { siteEnvironments } from './SiteDependencies';
import InfoTip from './InfoTip';

type Detail = Site & {documents: KnowledgeDoc[]; cases: PersonalCase[]};
const tabKeys = ['overview', 'issues', 'manuals', 'history'];

export default function Websites({assist, ask, knowledge, initialId, opened,onTitle}: {onTitle?:(id:string,name:string)=>void;ask?:(entry:SupportEntry)=>void;initialId?:string; opened?:(id:string)=>void; assist: (entry: SupportEntry) => void; knowledge: () => void}) {
  const guest=!!savedAuth()?.is_guest;
  const {message} = App.useApp(), navigate = useNavigate(), [params, setParams] = useSearchParams();
  const [sites, setSites] = useState<Site[]>([]), [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState<Site | undefined>(), [editor, setEditor] = useState(false), [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false), [loadError, setLoadError] = useState('');
  const [page,setPage]=useState(1),[total,setTotal]=useState(0),[search,setSearch]=useState('');
  const [environment,setEnvironment]=useState<string>(),[group,setGroup]=useState(''),[owner,setOwner]=useState('');
  const [deleting, setDeleting] = useState<Site | null>(null), [deleteRecords, setDeleteRecords] = useState(false);
  const currentId = useRef(initialId), request = useRef(0);
  currentId.current = initialId;
  const tab = params.get('tab') || 'overview', activeTab = tabKeys.includes(tab) ? tab : 'overview';
  function changeTab(key: string) {
    setParams(previous => {const next = new URLSearchParams(previous); next.set('tab', key); return next;});
  }
  function openSite(id: string) {if (opened) opened(id); else navigate('/websites/' + id);}
  useEffect(()=>{if(!guest&&!initialId&&params.get('create')==='1'){setEditing(undefined);setEditor(true);setParams(previous=>{const next=new URLSearchParams(previous);next.delete('create');return next;},{replace:true});}},[params,guest,initialId,setParams]);
  const refresh=useCallback(async()=>{const query=new URLSearchParams({page:String(page),page_size:'6',q:search});if(environment)query.set('environment',environment);if(group)query.set('group',group);if(owner)query.set('owner',owner);const r=await api<PageData<Site>>('/api/workspace/sites?'+query);setSites(r.items);setTotal(r.total);},[page,search,environment,group,owner]);
  const loadSite = useCallback(async (id: string) => {
    if (currentId.current !== id) return;
    const sequence = ++request.current;
    setLoading(true); setLoadError('');
    try {
      const result = await api<Detail>(`/api/support/sites/${id}`);
      // 迟到的详情只能被丢弃，不能覆盖新网站或触发路由跳转。
      if (request.current === sequence && currentId.current === id) {setDetail(result);onTitle?.(result.id,result.name);}
    } catch (error) {
      if (request.current === sequence && currentId.current === id) setLoadError((error as Error).message);
    } finally {
      if (request.current === sequence && currentId.current === id) setLoading(false);
    }
  }, [onTitle]);
  useEffect(() => {const timer=setTimeout(()=>refresh().catch(e => message.error(e.message)),200);return()=>clearTimeout(timer);}, [refresh, message]);
  useEffect(() => {
    setDetail(null); setLoadError('');
    if (initialId) void loadSite(initialId); else setLoading(false);
    return () => {request.current++;};
  }, [initialId, loadSite]);
  async function remove() {
    if (!deleting) return; setBusy(true);
    try {
      await api(`/api/support/sites/${deleting.id}?delete_records=${deleteRecords}`, {method:'DELETE'});
      if (currentId.current === deleting.id) navigate('/websites');
      setDeleting(null); await refresh(); message.success('网站已删除');
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  async function checkHttp(site: Detail) {
    setBusy(true);
    try {
      await post(`/api/maintenance/sites/${site.id}/http-check`);
      if (currentId.current === site.id) {await loadSite(site.id); message.success('本次HTTP响应已记录');}
      await refresh();
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  async function configureMonitor(site: Detail, enabled: boolean, interval_minutes: number) {
    setBusy(true);
    try {
      const monitor=await api<HttpMonitor>('/api/maintenance/sites/'+site.id+'/http-monitor',
        {method:'PUT',body:JSON.stringify({enabled,interval_minutes})});
      setDetail(previous=>previous?.id===site.id?{...previous,http_monitor:monitor}:previous);
      message.success(enabled?'定时巡检已开启':'定时巡检已暂停');
    } catch(error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  const site = detail?.id === initialId ? detail : null;
  const monitor = site?.http_monitor;
  const lastProbe = monitor?.last_result;
  const problems = site?.cases || [];
  const unarchived = problems.filter(c => !c.archived);
  const awaiting = unarchived.filter(c => c.status === 'awaiting_results');
  const actionable = unarchived.filter(c => c.status !== 'user_resolved');

  return <div className="page-stack websites-page">
    {!initialId ? <>
      <div className="page-title"><h2>我的网站 <InfoTip label="网站运行状态说明">网站运行状态以你提交的证据为准，尚未接入持续监控。</InfoTip></h2><Button type="primary" disabled={guest} onClick={() => {setEditing(undefined); setEditor(true);}}>添加网站</Button></div>
      <div className="site-filters"><Input.Search aria-label="搜索网站" placeholder="搜索网站或环境" onSearch={v=>{setSearch(v);setPage(1);}} allowClear /><Select aria-label="按运行环境筛选网站" allowClear placeholder="全部环境" value={environment} onChange={value=>{setEnvironment(value);setPage(1);}} options={Object.entries(siteEnvironments).map(([value,label])=>({value,label}))}/><Input aria-label="按业务分组筛选网站" placeholder="业务分组" allowClear value={group} onChange={e=>{setGroup(e.target.value);setPage(1);}}/><Input aria-label="按负责人筛选网站" placeholder="负责人" allowClear value={owner} onChange={e=>{setOwner(e.target.value);setPage(1);}}/></div>
      {!sites.length && <Card><Empty description="还没有符合条件的网站"><Button disabled={guest} onClick={() => {setEditing(undefined); setEditor(true);}}>添加我的网站</Button></Empty></Card>}
      <div className="home-cards">{sites.map(s => <Card key={s.id} title={s.name}>
        <Space wrap className="section-bottom"><Tag>{siteEnvironments[s.environment||'unknown']||'未分类'}</Tag>{s.group&&<Tag>{s.group}</Tag>}{s.owner&&<span className="muted">负责人：{s.owner}</span>}</Space><p>{[s.framework, s.deployment, s.database].filter(Boolean).join(' · ') || '环境信息尚未填写'}</p><Space wrap><Tag>{s.problem_count || 0} 个问题</Tag><Tag color={s.open_count ? 'orange' : 'default'}>{s.open_count || 0} 个待处理</Tag><Tag>{s.doc_ids.length} 份关联资料</Tag><Tag>未接入实时监控</Tag></Space>
        <p className="muted">最近活动：{s.last_activity_at?formatTime(s.last_activity_at):'暂无'}<br/>最近提交检查：{s.last_check_at?formatTime(s.last_check_at):'暂无结果'} · 入库失败 {s.document_tasks?.failed || 0}</p><div className="section-gap"><Space wrap><Button onClick={() => openSite(s.id)}>打开网站</Button><Button type="link" disabled={guest} onClick={() => assist({siteId: s.id, workflow: 'diagnose'})}>排查新问题</Button><Button danger type="text" disabled={guest||busy} onClick={()=>{setDeleting(s);setDeleteRecords(false);}}>删除网站</Button></Space></div>
      </Card>)}</div>
      {total>6&&<Pagination current={page} total={total} pageSize={6} onChange={setPage} />}
    </> : <>
      {loading && <div className="site-loading" role="status"><Spin/><span>正在加载网站详情</span></div>}
      {loadError && <Alert type="error" showIcon title="网站详情加载失败" description={loadError} action={<Button onClick={() => void loadSite(initialId)}>重试</Button>}/>}
      {site && <>
        <div className="page-title site-heading"><div><h2>{site.name}</h2><Space wrap>{[site.framework,site.deployment,site.database].filter(Boolean).map((value,index)=><Tag key={index}>{value}</Tag>)}</Space></div>
          <Space wrap>{ask&&<Button onClick={()=>ask({siteId:site.id,workflow:'diagnose'})}>在旁边提问</Button>}<Button disabled={guest} onClick={() => {setEditing(site); setEditor(true);}}>编辑网站</Button><Button type="primary" disabled={guest} onClick={() => assist({siteId:site.id,workflow:'diagnose'})}>分析这个网站</Button></Space>
        </div>
        <div className="site-stats" aria-label="网站记录概况">
          {[['问题记录',problems.length],['未归档问题',unarchived.length],['等待操作反馈',awaiting.length],['维护资料',site.documents.length]].map(([label,value])=><div className="site-stat" key={label}><span>{label}</span><strong>{value}</strong></div>)}
        </div>
        <Tabs className="site-tabs" activeKey={activeTab} onChange={changeTab} destroyOnHidden items={[
          {key:'overview',label:'概况',children:<div className="page-stack">
            <div className="site-overview-grid">
              <Card title="网站环境"><Descriptions column={{xs:1,sm:2,md:2}} items={[
                {key:'environment',label:'运行环境',children:siteEnvironments[site.environment||'unknown']||'未分类'},
                {key:'group',label:'业务分组',children:site.group||'未填写'},
                {key:'owner',label:'负责人',children:site.owner||'未填写'},
                {key:'url',label:'公开网址',children:site.url || '未填写'},
                {key:'framework',label:'框架',children:site.framework || '未填写'},
                {key:'deploy',label:'部署',children:site.deployment || '未填写'},
                {key:'database',label:'数据库',children:site.database || '未填写'},
                {key:'versions',label:'版本',children:site.versions || '未填写'},
                {key:'notes',label:'维护说明',children:site.notes || '暂无'},
              ]}/></Card>
              <Card title={<>需要处理 <InfoTip label="网站问题说明">未归档、用户确认解决和网站运行状态分别记录。</InfoTip></>} extra={<Button type="link" onClick={() => changeTab('issues')}>查看全部问题</Button>}>
                {actionable.length ? <div className="site-action-list">{actionable.slice(0,3).map(c=><div key={c.id}><Button type="link" className="site-case-link" onClick={() => assist({caseId:c.id,siteId:site.id})}>{c.title || c.problem?.symptom || '未命名问题'}</Button><StatusTag status={c.status}/><small>{formatTime(c.updated_at || c.created_at)}</small></div>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无需要处理的问题"/>}
              </Card>
            </div>
            <Card title={<>组件依赖 <InfoTip label="组件依赖说明">手工登记的组件关系，箭头表示依赖方向，不代表实时连接或运行状态。点击组件查看直接关联。</InfoTip></>}><SiteDependencies key={site.id} site={site} ask={ask}/></Card>
            <Card title={<>单次 HTTP 检查 <InfoTip label="单次HTTP检查说明">只记录本次公开网址的 HTTP 响应，不代表持续监控或故障已解决。{!site.url && '请先在“编辑网站”中填写公开网址。'}</InfoTip></>} extra={<Button loading={busy} disabled={guest||!site.url} onClick={() => void checkHttp(site)}>主动检查HTTP响应</Button>}>

              {site.health_check ? <div><p>最近单次HTTP检查：{({normal:'响应正常',abnormal:'响应异常',unknown:'结果未知'} as Record<string,string>)[site.health_check.state] || '结果未知'} · HTTP {site.health_check.http_status || '未取得'} · {formatTime(site.health_check.collected_at)}<br/><small>{site.health_check.scope}</small></p><Button onClick={()=>copyText(JSON.stringify(site.health_check,null,2)).then(()=>message.success('已复制检查结果，可在排障时提交')).catch(()=>message.error('复制失败，请手动记录检查结果'))}>复制这次结果</Button></div> : <p>尚未主动检查</p>}
            </Card>
            <Card title="定时 HTTP 巡检" extra={<Switch aria-label="启用定时 HTTP 巡检" checked={!!monitor?.enabled}
              loading={busy} disabled={guest||(!site.url&&!monitor?.enabled)}
              onChange={enabled=>void configureMonitor(site,enabled,monitor?.interval_minutes || 15)}/>}>
              <Space wrap><span>检查间隔</span><Select aria-label="巡检间隔" value={monitor?.interval_minutes || 15}
                disabled={guest||busy||!site.url} onChange={minutes=>void configureMonitor(site,!!monitor?.enabled,minutes)}
                options={[5,15,30,60].map(value=>({value,label:value+' 分钟'}))}/>
                <Button type="link" onClick={()=>void loadSite(site.id)}>刷新巡检状态</Button></Space>
              <p>连续 {monitor?.failure_threshold || 3} 次异常响应或超时会登记问题。</p>
              {monitor?.enabled&&monitor.next_check_at&&<p>下次计划检查 {formatTime(monitor.next_check_at)}</p>}
              {lastProbe&&<p>最近巡检 {formatTime(lastProbe.collected_at)} · {lastProbe.http_status
                ? 'HTTP '+lastProbe.http_status : lastProbe.timed_out?'请求超时':'未取得有效响应'}
                {' · 连续异常 '+(monitor?.consecutive_failures || 0)+' 次'}</p>}
              {monitor?.case_id&&<Button onClick={()=>navigate('/issues/'+monitor.case_id)}>查看巡检发现的问题</Button>}
            </Card>
            <Space wrap><Button onClick={() => assist({siteId:site.id,workflow:'consult'})}>咨询维护知识</Button><Button onClick={() => assist({siteId:site.id,workflow:'history',question:'请查询这个网站以前保存的处理案例，说明哪些问题已确认解决，哪些只是归档。'})}>询问历史处理</Button></Space>
          </div>},
          {key:'issues',label:`问题记录（${problems.length}）`,children:<Card title={<>问题记录 <InfoTip label="归档与处理状态说明">归档与处理状态分别显示；用户确认解决后仍可保留为未归档记录。</InfoTip></>}>
            <Table<PersonalCase> className="site-issues-table" rowKey="id" dataSource={problems} pagination={{pageSize:8}} scroll={problems.length?{x:760}:undefined} locale={{emptyText:'这个网站还没有问题记录'}} columns={[
              {title:'问题',width:240,render:(_,c)=><strong>{c.title || c.problem?.symptom || '未命名问题'}</strong>},
              {title:'状态',width:180,render:(_,c)=><Space wrap><StatusTag status={c.status}/>{c.archived && <Tag>已归档</Tag>}</Space>},
              {title:'严重度',width:80,render:(_,c)=><SeverityTag value={c.severity}/>},
              {title:'创建时间',width:170,render:(_,c)=>formatTime(c.created_at)},
              {title:'操作',width:180,render:(_,c)=><Button onClick={() => assist({caseId:c.id,siteId:site.id})}>查看问题与处理过程</Button>},
            ]}/>
          </Card>},
          {key:'manuals',label:`维护资料（${site.documents.length}）`,children:<Card title={<>关联资料 <InfoTip label="网站关联资料说明">可直接使用内置官方指南；私人手册上传后，在编辑网站中选择适用资料。</InfoTip></>} extra={<Button onClick={knowledge}>上传 / 管理我的手册</Button>}>
            {site.documents.length ? <div className="site-document-list">{site.documents.map(d=><div key={d.id}><strong>{d.title}</strong><Tag>{d.chunk_count} 个有效片段</Tag></div>)}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有关联手册"/>}
          </Card>},
          {key:'history',label:'维护历史',children:<MaintenanceTimeline key={site.id} siteId={site.id}/>},
        ]}/>
      </>}
    </>}
    <SiteEditor open={editor} site={editing} close={() => setEditor(false)} saved={s => {
      setEditor(false); refresh().catch(e => message.error(e.message));
      if (currentId.current === s.id) void loadSite(s.id); else openSite(s.id);
    }}/>
    <Modal open={!!deleting} title={`删除网站「${deleting?.name || ''}」？`} onCancel={()=>setDeleting(null)} onOk={remove} confirmLoading={busy} okText="确认删除" cancelText="取消" okButtonProps={{danger:true}}>
      <p>默认保留问题和维护对话，解除网站关联并保留当时的环境快照。上传的手册不会被删除。</p>
      <Checkbox checked={deleteRecords} onChange={e=>setDeleteRecords(e.target.checked)}>同时删除这个网站关联的问题和维护对话</Checkbox><p className="muted">记录从工作区移除，关联审批与执行审计保留。</p>
    </Modal>
  </div>;
}
