import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Checkbox, Descriptions, Empty, Modal, Pagination, Input, Space, Table, Tag } from 'antd';
import { api, copyText, post, formatTime } from './api';
import type { KnowledgeDoc, PageData, PersonalCase, Site, SupportEntry } from './types';
import { caseStates } from './supportTypes';
import SiteEditor from './SiteEditor';
import MaintenanceTimeline from './MaintenanceTimeline';

type Detail = Site & {documents: KnowledgeDoc[]; cases: PersonalCase[]};
export default function Websites({assist, knowledge, initialId, opened}: {initialId?:string; opened?:(id:string)=>void; assist: (entry: SupportEntry) => void; knowledge: () => void}) {
  const {message} = App.useApp();
  const [sites, setSites] = useState<Site[]>([]), [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState<Site | undefined>(), [editor, setEditor] = useState(false), [busy, setBusy] = useState(false);
  const [page,setPage]=useState(1),[total,setTotal]=useState(0),[search,setSearch]=useState('');
  const [deleting, setDeleting] = useState<Site | null>(null), [deleteRecords, setDeleteRecords] = useState(false);
  const refresh=useCallback(async()=>{const r=await api<PageData<Site>>(`/api/workspace/sites?page=${page}&page_size=6&q=${encodeURIComponent(search)}`);setSites(r.items);setTotal(r.total);},[page,search]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  useEffect(()=>{if(initialId && detail?.id!==initialId)void select(initialId);else if(!initialId)setDetail(null);},[initialId]);
  async function select(id: string) {setBusy(true);setDetail(null); try {setDetail(await api<Detail>(`/api/support/sites/${id}`)); opened?.(id);} catch (e) {message.error((e as Error).message);} finally {setBusy(false);}}
  async function remove() {
    if (!deleting) return; setBusy(true);
    try {await api(`/api/support/sites/${deleting.id}?delete_records=${deleteRecords}`, {method:'DELETE'}); if (detail?.id === deleting.id) setDetail(null); setDeleting(null); await refresh(); message.success('网站已删除');}
    catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="page-stack"><div className="page-title"><div><h2>我的网站</h2><p>集中管理环境、维护资料与问题记录。网站运行状态以你提交的证据为准。</p></div><Button type="primary" onClick={() => {setEditing(undefined); setEditor(true);}}>添加网站</Button></div>
    <Input.Search aria-label="搜索网站" placeholder="搜索网站或环境" onSearch={v=>{setSearch(v);setPage(1);}} allowClear />
    {!sites.length && <Card><Empty description="先添加一个网站，或直接向助手提问" /><Space><Button onClick={() => {setEditing(undefined); setEditor(true);}}>添加我的网站</Button><Button onClick={() => assist({workflow: 'consult'})}>先咨询维护问题</Button></Space></Card>}
    <div className="home-cards">{sites.map(s => <Card key={s.id} title={s.name} className={detail?.id === s.id ? 'selected-site' : ''}>
      <p>{[s.framework, s.deployment, s.database].filter(Boolean).join(' · ') || '环境信息尚未填写'}</p><Space wrap><Tag>{s.problem_count || 0} 个问题</Tag><Tag color={s.open_count ? 'orange' : 'default'}>{s.open_count || 0} 个待处理</Tag><Tag>{s.doc_ids.length} 份关联资料</Tag><Tag>未接入实时监控</Tag></Space>
      <p className="muted">最近活动：{s.last_activity_at?formatTime(s.last_activity_at):'暂无'}<br/>最近提交检查：{s.last_check_at?formatTime(s.last_check_at):'暂无结果'} · 入库失败 {s.document_tasks?.failed || 0}</p><div className="section-gap"><Space wrap><Button loading={busy && detail?.id === s.id} onClick={() => select(s.id)}>打开网站</Button><Button type="link" onClick={() => assist({siteId: s.id, workflow: 'diagnose'})}>排查新问题</Button><Button danger type="text" disabled={busy} onClick={()=>{setDeleting(s);setDeleteRecords(false);}}>删除网站</Button></Space></div>
    </Card>)}</div>
    {total>6&&<Pagination current={page} total={total} pageSize={6} onChange={setPage} />}
    {detail && <><Card title={detail.name} extra={<Button onClick={() => {setEditing(sites.find(s => s.id === detail.id)); setEditor(true);}}>编辑网站</Button>}>
      <Descriptions column={2} items={[{key:'framework',label:'框架',children:detail.framework || '未填写'},{key:'deploy',label:'部署',children:detail.deployment || '未填写'},{key:'database',label:'数据库',children:detail.database || '未填写'},{key:'versions',label:'版本',children:detail.versions || '未填写'},{key:'notes',label:'维护说明',children:detail.notes || '暂无'}]} />
      <Space wrap><Button disabled={!detail.url || busy} onClick={async()=>{setBusy(true);try{await post(`/api/maintenance/sites/${detail.id}/http-check`);await select(detail.id);await refresh();message.success('本次HTTP响应已记录');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}}>主动检查HTTP响应</Button><Button type="primary" onClick={() => assist({siteId: detail.id, workflow: 'diagnose'})}>为这个网站排障</Button><Button onClick={() => assist({siteId: detail.id, workflow: 'consult'})}>咨询维护知识</Button><Button onClick={() => assist({siteId: detail.id, workflow: 'history', question: '请查询这个网站以前保存的处理案例，说明哪些问题已确认解决，哪些只是归档。'})}>询问历史处理</Button></Space>
      {detail.health_check&&<p>最近单次HTTP检查：{({normal:'响应正常',abnormal:'响应异常',unknown:'结果未知'} as Record<string,string>)[detail.health_check.state]} · HTTP {detail.health_check.http_status || '未取得'} · {formatTime(detail.health_check.collected_at)}<br/><small>{detail.health_check.scope}</small><br/><Button size="small" onClick={()=>copyText(JSON.stringify(detail.health_check,null,2)).then(()=>message.success('已复制检查结果，可在排障时提交')).catch(()=>message.error('复制失败，请手动记录检查结果'))}>复制这次结果</Button></p>}
    </Card><Card title="关联资料" extra={<Button onClick={knowledge}>上传 / 管理我的手册</Button>}>
      {detail.documents.length ? <Space wrap>{detail.documents.map(d => <Tag key={d.id}>{d.title} · {d.chunk_count}个有效片段</Tag>)}</Space> : <p className="muted">还没有关联手册，仍可使用内置官方来源指南。上传后在“编辑网站”中关联。</p>}
    </Card><Card title="这个网站的问题与维护记录">
      <Table<PersonalCase> rowKey="id" dataSource={detail.cases} pagination={{pageSize: 8}} columns={[
        {title:'问题',dataIndex:'title'}, {title:'状态',render:(_, c) => <Space><Tag>{caseStates[c.status] || c.status}</Tag>{c.archived && <Tag>已归档</Tag>}</Space>},
        {title:'创建时间',render:(_, c) => formatTime(c.created_at)}, {title:'操作',render:(_, c) => <Button onClick={() => assist({caseId: c.id})}>查看问题与处理过程</Button>},
      ]} />
    </Card><MaintenanceTimeline siteId={detail.id}/></>}
    <SiteEditor open={editor} site={editing} close={() => setEditor(false)} saved={s => {setEditor(false); refresh().catch(e => message.error(e.message)); void select(s.id);}} />
    <Modal open={!!deleting} title={`删除网站「${deleting?.name || ''}」？`} onCancel={()=>setDeleting(null)} onOk={remove} confirmLoading={busy} okText="确认删除" cancelText="取消" okButtonProps={{danger:true}}>
      <p>默认保留问题和维护对话，解除网站关联并保留当时的环境快照。上传的手册不会被删除。</p>
      <Checkbox checked={deleteRecords} onChange={e=>setDeleteRecords(e.target.checked)}>同时删除这个网站关联的问题和维护对话</Checkbox><p className="muted">记录从工作区移除，关联审批与执行审计保留。</p>
    </Modal>
  </div>;
}
