import { useCallback, useEffect, useState } from 'react';
import { Alert, App, Button, Checkbox, Form, Input, Modal, Select, Space, Table, Tag } from 'antd';
import { api, formatTime, post } from './api';
import type { PersonalCase, Site, SupportEntry } from './types';
import { caseStates } from './supportTypes';

export default function Issues({assist}: {assist: (entry: SupportEntry) => void}) {
  const {message} = App.useApp(), [form] = Form.useForm();
  const [records, setRecords] = useState<PersonalCase[]>([]), [sites, setSites] = useState<Site[]>([]), [site, setSite] = useState<string>();
  const [status, setStatus] = useState<string>(), [archive, setArchive] = useState('active'), [search, setSearch] = useState('');
  const [selected, setSelected] = useState<PersonalCase | null>(null), [action, setAction] = useState<'create'|'edit'|'resolve'|'archive'|null>(null);
  const [note, setNote] = useState(''), [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    const query = new URLSearchParams({kind:'problem'});
    if (site) query.set('site_id',site); if (status) query.set('status',status); if (archive !== 'all') query.set('archived',String(archive === 'archived'));
    setRecords(await api<PersonalCase[]>('/api/support/cases?' + query));
  }, [site,status,archive]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh,message]);
  useEffect(() => {api<Site[]>('/api/support/sites').then(setSites).catch(e => message.error(e.message));}, [message]);
  function open(c: PersonalCase, mode: 'edit'|'resolve'|'archive') {setSelected(c); setAction(mode); setConfirmed(false); setNote(c.solution || ''); form.resetFields(); form.setFieldsValue({title:c.title,...c.problem});}
  function create() {setSelected(null); setAction('create'); form.resetFields(); form.setFieldsValue({site_id:site});}
  async function submit() {
    if (!selected && action !== 'create') return;
    try {
      const values = action === 'edit' || action === 'create' ? await form.validateFields() : null; setBusy(true);
      if (action === 'create') await post('/api/support/cases',{...values,site_id:values.site_id || null,kind:'problem'});
      if (action === 'edit' && selected) await api(`/api/support/cases/${selected.id}/problem`,{method:'PUT',body:JSON.stringify(values)});
      if (action === 'resolve' && selected) await post(`/api/support/cases/${selected.id}/status`,{status:'user_resolved',note,user_confirmed:confirmed});
      if (action === 'archive' && selected) await post(`/api/support/cases/${selected.id}/save`,{solution:note,user_confirmed:confirmed});
      setAction(null); await refresh(); message.success('问题记录已更新');
    } catch(e) {if (e instanceof Error) message.error(e.message);} finally {setBusy(false);}
  }
  async function reopen(id: string) {setBusy(true); try {await post(`/api/support/cases/${id}/status`,{status:'reopen'}); await refresh(); message.success('已重新打开，原证据和资料版本保留');} catch(e) {message.error((e as Error).message);} finally {setBusy(false);}}
  return <div className="page-stack"><div className="page-title"><div><h2>问题中心</h2><p>查看异常、检查进度和处理结果。归档保留记录；“用户确认解决”与“样本通过”分别展示。</p></div><Button type="primary" onClick={create}>新建排障问题</Button></div>
    <Space wrap><Select aria-label="按网站筛选问题" allowClear placeholder="全部网站" value={site} onChange={setSite} style={{width:190}} options={sites.map(s => ({value:s.id,label:s.name}))} /><Select aria-label="按问题状态筛选" allowClear placeholder="全部问题状态" value={status} onChange={setStatus} style={{width:190}} options={Object.entries(caseStates).map(([value,label]) => ({value,label}))} /><Select aria-label="归档筛选" value={archive} onChange={setArchive} options={[{value:'active',label:'未归档'},{value:'archived',label:'已归档'},{value:'all',label:'全部记录'}]} /><Input.Search aria-label="搜索问题" placeholder="问题标题或异常关键词" value={search} onChange={e => setSearch(e.target.value)} style={{width:220}} /></Space>
    <Table<PersonalCase> rowKey="id" dataSource={records.filter(c => ((c.title || '') + (c.problem?.symptom || '') + (c.summary || '')).toLowerCase().includes(search.toLowerCase()))} pagination={{pageSize:10}} scroll={{x:850}} expandable={{expandedRowRender:c => <><p><strong>异常：</strong>{c.problem?.symptom || '尚未补充'}</p><p><strong>接口：</strong>{c.problem?.endpoint || '未指定'}　<strong>发生时间：</strong>{c.problem?.started_at || '未知'}</p><p><strong>影响：</strong>{c.problem?.impact || '未填写'}　<strong>近期变更：</strong>{c.problem?.recent_changes || '未填写'}</p><p>{c.summary}</p>{c.solution && <p><strong>用户处理记录：</strong>{c.solution}</p>}</>}} columns={[
      {title:'问题',dataIndex:'title'}, {title:'网站',dataIndex:'site_name'}, {title:'状态',render:(_,c) => <Space wrap><Tag color={c.status === 'user_resolved' ? 'green' : 'default'}>{caseStates[c.status] || c.status}</Tag>{c.archived && <Tag>已归档</Tag>}</Space>}, {title:'创建',render:(_,c) => formatTime(c.created_at)},
      {title:'操作',width:300,render:(_,c) => <Space wrap><Button size="small" onClick={() => assist({caseId:c.id})}>查看 / 继续</Button>{!c.archived && c.status !== 'user_resolved' && <><Button size="small" onClick={() => open(c,'edit')}>问题信息</Button><Button size="small" onClick={() => open(c,'resolve')}>确认解决</Button></>}{!c.archived && <Button size="small" onClick={() => open(c,'archive')}>归档</Button>}{(c.archived || c.status === 'user_resolved') && <Button size="small" loading={busy} onClick={() => reopen(c.id)}>重新打开</Button>}</Space>},
    ]} />
    <Modal open={!!action} title={action === 'create' ? '创建网站问题记录' : action === 'edit' ? '维护问题信息' : action === 'resolve' ? '由你确认问题已解决' : '归档处理记录'} onCancel={() => setAction(null)} onOk={submit} confirmLoading={busy} okButtonProps={{disabled: action !== 'edit' && action !== 'create' && (!confirmed || !note.trim())}}>
      {action === 'edit' || action === 'create' ? <Form form={form} layout="vertical">{action === 'create' && <Form.Item name="site_id" label="所属网站（选填）"><Select allowClear options={sites.map(s=>({value:s.id,label:s.name}))} /></Form.Item>}<Form.Item name="title" label="问题标题" rules={[{required:true}]}><Input maxLength={100} placeholder="例如：结算页返回502" /></Form.Item><Form.Item name="symptom" label="异常表现"><Input.TextArea rows={3} maxLength={2000} /></Form.Item><Form.Item name="endpoint" label="原失败接口"><Input placeholder="/orders" maxLength={200} /></Form.Item><Form.Item name="started_at" label="发生时间 / 时区"><Input maxLength={100} /></Form.Item><Form.Item name="impact" label="影响范围"><Input maxLength={500} /></Form.Item><Form.Item name="recent_changes" label="近期变更"><Input.TextArea rows={2} maxLength={1000} /></Form.Item></Form> : <><Alert type="info" title={action === 'resolve' ? '这是你对自己网站的确认，平台不会把它伪装成自动监控结论。' : '归档不会将问题标记为已解决；未解决的问题也可以暂存并稍后重新打开。'} /><Input.TextArea className="section-gap" aria-label="处理结局说明" rows={5} maxLength={2000} value={note} onChange={e => setNote(e.target.value)} placeholder="实际操作、结果、遗留问题或暂停原因" /><Checkbox className="section-gap" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>我已核对并确认这份处理记录</Checkbox></>}
    </Modal>
  </div>;
}
