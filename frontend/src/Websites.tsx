import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Checkbox, Descriptions, Empty, Modal, Space, Table, Tag } from 'antd';
import { api, formatTime } from './api';
import type { KnowledgeDoc, PersonalCase, Site, SupportEntry } from './types';
import { caseStates } from './supportTypes';
import SiteEditor from './SiteEditor';

type Detail = Site & {documents: KnowledgeDoc[]; cases: PersonalCase[]};
export default function Websites({assist, knowledge}: {assist: (entry: SupportEntry) => void; knowledge: () => void}) {
  const {message} = App.useApp();
  const [sites, setSites] = useState<Site[]>([]), [detail, setDetail] = useState<Detail | null>(null);
  const [editing, setEditing] = useState<Site | undefined>(), [editor, setEditor] = useState(false), [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<Site | null>(null), [deleteRecords, setDeleteRecords] = useState(false);
  const refresh = useCallback(async () => setSites(await api<Site[]>('/api/support/sites')), []);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  async function select(id: string) {setBusy(true); try {setDetail(await api<Detail>(`/api/support/sites/${id}`));} catch (e) {message.error((e as Error).message);} finally {setBusy(false);}}
  async function remove() {
    if (!deleting) return; setBusy(true);
    try {await api(`/api/support/sites/${deleting.id}?delete_records=${deleteRecords}`, {method:'DELETE'}); if (detail?.id === deleting.id) setDetail(null); setDeleting(null); await refresh(); message.success('网站已删除');}
    catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="page-stack"><div className="page-title"><div><h2>我的网站</h2><p>集中管理环境、维护资料与问题记录。网站运行状态以你提交的证据为准。</p></div><Button type="primary" onClick={() => {setEditing(undefined); setEditor(true);}}>添加网站</Button></div>
    {!sites.length && <Card><Empty description="先添加一个网站，或直接向助手提问" /><Space><Button onClick={() => {setEditing(undefined); setEditor(true);}}>添加我的网站</Button><Button onClick={() => assist({workflow: 'consult'})}>先咨询维护问题</Button></Space></Card>}
    <div className="home-cards">{sites.map(s => <Card key={s.id} title={s.name} className={detail?.id === s.id ? 'selected-site' : ''}>
      <p>{[s.framework, s.deployment, s.database].filter(Boolean).join(' · ') || '环境信息尚未填写'}</p><Space wrap><Tag>{s.problem_count || 0} 个问题</Tag><Tag color={s.open_count ? 'orange' : 'default'}>{s.open_count || 0} 个待处理</Tag><Tag>{s.doc_ids.length} 份关联资料</Tag></Space>
      <div className="section-gap"><Space wrap><Button loading={busy && detail?.id === s.id} onClick={() => select(s.id)}>打开网站</Button><Button type="link" onClick={() => assist({siteId: s.id, workflow: 'diagnose'})}>排查新问题</Button><Button danger type="text" disabled={busy} onClick={()=>{setDeleting(s);setDeleteRecords(false);}}>删除网站</Button></Space></div>
    </Card>)}</div>
    {detail && <><Card title={detail.name} extra={<Button onClick={() => {setEditing(sites.find(s => s.id === detail.id)); setEditor(true);}}>编辑网站</Button>}>
      <Descriptions column={2} items={[{key:'framework',label:'框架',children:detail.framework || '未填写'},{key:'deploy',label:'部署',children:detail.deployment || '未填写'},{key:'database',label:'数据库',children:detail.database || '未填写'},{key:'versions',label:'版本',children:detail.versions || '未填写'},{key:'notes',label:'维护说明',children:detail.notes || '暂无'}]} />
      <Space wrap><Button type="primary" onClick={() => assist({siteId: detail.id, workflow: 'diagnose'})}>为这个网站排障</Button><Button onClick={() => assist({siteId: detail.id, workflow: 'consult'})}>咨询维护知识</Button><Button onClick={() => assist({siteId: detail.id, workflow: 'history', question: '请查询这个网站以前保存的处理案例，说明哪些问题已确认解决，哪些只是归档。'})}>询问历史处理</Button></Space>
    </Card><Card title="关联资料" extra={<Button onClick={knowledge}>上传 / 管理我的手册</Button>}>
      {detail.documents.length ? <Space wrap>{detail.documents.map(d => <Tag key={d.id}>{d.title} · {d.chunk_count}个有效片段</Tag>)}</Space> : <p className="muted">还没有关联手册，仍可使用内置官方来源指南。上传后在“编辑网站”中关联。</p>}
    </Card><Card title="这个网站的问题与维护记录">
      <Table<PersonalCase> rowKey="id" dataSource={detail.cases} pagination={{pageSize: 8}} columns={[
        {title:'问题',dataIndex:'title'}, {title:'状态',render:(_, c) => <Space><Tag>{caseStates[c.status] || c.status}</Tag>{c.archived && <Tag>已归档</Tag>}</Space>},
        {title:'创建时间',render:(_, c) => formatTime(c.created_at)}, {title:'操作',render:(_, c) => <Button onClick={() => assist({caseId: c.id})}>查看问题与处理过程</Button>},
      ]} />
    </Card></>}
    <SiteEditor open={editor} site={editing} close={() => setEditor(false)} saved={s => {setEditor(false); refresh().catch(e => message.error(e.message)); void select(s.id);}} />
    <Modal open={!!deleting} title={`删除网站「${deleting?.name || ''}」？`} onCancel={()=>setDeleting(null)} onOk={remove} confirmLoading={busy} okText="确认删除" cancelText="取消" okButtonProps={{danger:true}}>
      <p>默认保留问题和维护对话，解除网站关联并保留当时的环境快照。上传的手册不会被删除。</p>
      <Checkbox checked={deleteRecords} onChange={e=>setDeleteRecords(e.target.checked)}>同时删除这个网站关联的问题和维护对话</Checkbox><p className="muted">记录从工作区移除，关联审批与执行审计保留。</p>
    </Modal>
  </div>;
}
