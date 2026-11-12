import { useCallback, useEffect, useState } from 'react';
import { Alert, App, Button, Checkbox, Dropdown, Empty, Form, Input, Modal, Segmented, Select, Space, Table, Tag } from 'antd';
import { MoreOutlined, PlusOutlined } from '@ant-design/icons';
import { api, formatTime, post } from './api';
import type { PageData, PersonalCase, Site, SupportEntry } from './types';
import { caseStates } from './supportTypes';
import DeleteRecordsButton from './DeleteRecordsButton';

export default function Issues({assist}: {assist: (entry: SupportEntry) => void}) {
  const {message} = App.useApp(), [form] = Form.useForm();
  const [records, setRecords] = useState<PersonalCase[]>([]), [sites, setSites] = useState<Site[]>([]), [site, setSite] = useState<string>();
  const [status, setStatus] = useState<string>(), [archive, setArchive] = useState('active'), [search, setSearch] = useState('');
  const [page,setPage]=useState(1),[total,setTotal]=useState(0),[loading,setLoading]=useState(true);
  const [severity,setSeverity]=useState<string>();
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selected, setSelected] = useState<PersonalCase | null>(null), [action, setAction] = useState<'create'|'edit'|'resolve'|'archive'|null>(null);
  const [note, setNote] = useState(''), [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    setLoading(true);
    try{
      const query = new URLSearchParams({case_kind:'problem',page:String(page),page_size:'10',q:search});
      if(severity)query.set('severity',severity);
      if(site)query.set('site_id',site);if(status)query.set('status',status);if(archive!=='all')query.set('archived',String(archive==='archived'));
      const result=await api<PageData<PersonalCase>>('/api/workspace/cases?'+query);setRecords(result.items);setTotal(result.total);
    }finally{setLoading(false);}
  }, [site,status,archive,page,search,severity]);
  useEffect(() => {const timer=setTimeout(()=>refresh().catch(e => message.error(e.message)),200);return()=>clearTimeout(timer);}, [refresh,message]);
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
  function moreItems(c:PersonalCase){
    const items=[];
    if(!c.archived&&c.status!=='user_resolved')items.push({key:'edit',label:'编辑问题信息'},{key:'resolve',label:'确认问题已解决'},{key:'archive',label:'归档记录'});
    if(c.archived||c.status==='user_resolved')items.push({key:'reopen',label:'重新打开问题'});
    return items;
  }
  return <div className="page-stack"><div className="page-title"><div><h2>问题中心</h2><p>集中处理异常、检查结果和用户确认。</p></div><Button type="primary" icon={<PlusOutlined />} onClick={create}>新建问题</Button></div>
    <div className="filter-bar"><Segmented aria-label="归档视图" value={archive} onChange={value=>{setArchive(String(value));setPage(1);}} options={[{value:'active',label:'处理中'},{value:'archived',label:'已归档'},{value:'all',label:'全部'}]} /><div className="filter-fields"><Input.Search aria-label="搜索问题" allowClear placeholder="搜索标题或异常" value={search} onChange={e=>{setSearch(e.target.value);setPage(1);}} /><Select aria-label="按严重度筛选" allowClear value={severity} placeholder="严重度" onChange={v=>{setSeverity(v);setPage(1);}} options={['P0','P1','P2','P3'].map(value=>({value,label:value}))}/><Select aria-label="按网站筛选问题" allowClear placeholder="全部网站" value={site} onChange={v=>{setSite(v);setPage(1);}} options={sites.map(s=>({value:s.id,label:s.name}))}/><Select aria-label="按问题状态筛选" allowClear placeholder="全部状态" value={status} onChange={v=>{setStatus(v);setPage(1);}} options={Object.entries(caseStates).map(([value,label])=>({value,label}))}/></div></div>
    {!!selectedIds.length&&<div className="bulk-bar"><span>已选择 {selectedIds.filter(id=>records.some(c=>c.id===id)).length} 项</span><DeleteRecordsButton ids={selectedIds.filter(id=>records.some(c=>c.id===id))} disabled={busy} label="删除选中" onDeleted={async()=>{setSelectedIds([]);await refresh();}} /></div>}
    <Table<PersonalCase> className="issues-table" rowKey="id" loading={loading} rowSelection={{selectedRowKeys:selectedIds.filter(id=>records.some(c=>c.id===id)),onChange:keys=>setSelectedIds(keys.map(String))}} dataSource={records} locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={search||site||status||severity?'没有符合筛选条件的问题':'暂无问题记录，可以直接向助手描述异常'}/>}} pagination={total?{current:page,pageSize:10,total,onChange:setPage,showSizeChanger:false}:false} scroll={{x:900}} onRow={c=>({onClick:()=>assist({caseId:c.id})})} columns={[
      {title:'问题',dataIndex:'title',width:310,render:(_,c)=><div className="issue-cell"><strong>{c.title||'未命名问题'}</strong><span>{c.problem?.symptom||c.summary||'尚未补充异常描述'}</span></div>},
      {title:'严重度',dataIndex:'severity',width:82,render:value=><Tag color={value==='P0'||value==='P1'?'red':value==='P2'?'orange':'default'}>{value||'P2'}</Tag>},
      {title:'网站',dataIndex:'site_name',width:150,render:value=>value||<span className="muted">未关联</span>},
      {title:'状态',width:150,render:(_,c)=><Space wrap size={4}><Tag color={c.status==='user_resolved'?'success':c.status==='awaiting_results'?'processing':'warning'}>{caseStates[c.status]||c.status}</Tag>{c.archived&&<Tag>已归档</Tag>}</Space>},
      {title:'最近更新',width:165,render:(_,c)=>formatTime(c.updated_at||c.created_at)},
      {title:'操作',width:145,fixed:'right',render:(_,c)=><Space size={4} onClick={e=>e.stopPropagation()}><Button size="small" type="link" onClick={()=>assist({caseId:c.id})}>{c.status==='user_resolved'?'查看结果':'继续处理'}</Button>{!!moreItems(c).length&&<Dropdown menu={{items:moreItems(c),onClick:({key})=>key==='reopen'?void reopen(c.id):open(c,key as 'edit'|'resolve'|'archive')}}><Button size="small" type="text" aria-label={`更多操作：${c.title}`} icon={<MoreOutlined />}/></Dropdown>}</Space>},
    ]} />
    <Modal open={!!action} title={action==='create'?'创建网站问题记录':action==='edit'?'维护问题信息':action==='resolve'?'由你确认问题已解决':'归档处理记录'} onCancel={()=>setAction(null)} onOk={submit} confirmLoading={busy} okButtonProps={{disabled:action!=='edit'&&action!=='create'&&(!confirmed||!note.trim())}}>
      {action==='edit'||action==='create'?<Form form={form} layout="vertical">{action==='create'&&<Form.Item name="site_id" label="所属网站（选填）"><Select allowClear options={sites.map(s=>({value:s.id,label:s.name}))}/></Form.Item>}<Form.Item name="title" label="问题标题" rules={[{required:true}]}><Input maxLength={100} placeholder="例如：结算页返回502"/></Form.Item><Form.Item name="symptom" label="异常表现"><Input.TextArea rows={3} maxLength={2000}/></Form.Item><Form.Item name="endpoint" label="原失败接口"><Input placeholder="/orders" maxLength={200}/></Form.Item><Form.Item name="started_at" label="发生时间 / 时区"><Input maxLength={100}/></Form.Item><Form.Item name="impact" label="影响范围"><Input maxLength={500}/></Form.Item><Form.Item name="recent_changes" label="近期变更"><Input.TextArea rows={2} maxLength={1000}/></Form.Item></Form>:<><Alert type="info" title={action==='resolve'?'这是你对自己网站的确认，平台不会把它伪装成自动监控结论。':'归档不会将问题标记为已解决；未解决的问题也可以暂存并稍后重新打开。'}/><Input.TextArea className="section-gap" aria-label="处理结局说明" rows={5} maxLength={2000} value={note} onChange={e=>setNote(e.target.value)} placeholder="实际操作、结果、遗留问题或暂停原因"/><Checkbox className="section-gap" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}>我已核对并确认这份处理记录</Checkbox></>}
    </Modal>
  </div>;
}
