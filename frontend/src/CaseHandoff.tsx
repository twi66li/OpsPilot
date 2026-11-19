import { useState } from 'react';
import { Alert, App, Button, Collapse, Drawer, Input, Modal, Space, Tag } from 'antd';
import { api, formatTime, post } from './api';
import type { HandoffStatus, PersonalCase, ReceivedHandoff } from './types';

const labels: Record<HandoffStatus['status'], string> = {
  pending: '待复核', reviewed: '已复核', changes_requested: '需补充',
};

export function HandoffEditor({record, changed}: {record:PersonalCase; changed:(status?:HandoffStatus)=>void}) {
  const {message} = App.useApp();
  const [open,setOpen] = useState(false), [busy,setBusy] = useState(false);
  const [recipient,setRecipient] = useState(''), [note,setNote] = useState('');
  const path = '/api/maintenance/cases/' + record.id + '/handoff';
  const current = record.handoff;

  async function update(revoke: boolean) {
    setBusy(true);
    try {
      if(revoke) {
        await api(path,{method:'DELETE'});
        changed();
        message.success('交接已撤回');
      } else {
        const status = await api<HandoffStatus>(path,{method:'PUT',body:JSON.stringify({recipient,note})});
        changed(status);setOpen(false);
        message.success('已交给成员复核');
      }
    } catch(error) {message.error((error as Error).message);} finally {setBusy(false);}
  }

  return <section aria-label="问题交接">
    <Space wrap>
      {current&&<><span>负责人 {current.recipient}</span><Tag>{labels[current.status]}</Tag></>}
      <Button size="small" onClick={()=>{setRecipient(current?.recipient || '');setNote(current?.note || '');setOpen(true);}}>
        {current?'重新交接':'交接复核'}
      </Button>
      {current&&<Button size="small" loading={busy} onClick={()=>void update(true)}>撤回交接</Button>}
    </Space>
    {current?.review_note&&<p>{current.review_note}</p>}
    <Modal open={open} title="交给团队成员复核" onCancel={()=>setOpen(false)} onOk={()=>void update(false)}
      okText="提交交接" confirmLoading={busy} okButtonProps={{disabled:!recipient.trim()||!note.trim()}}>
      <p>对方将在问题中心看到这个问题的证据快照、当前判断和复测结论。提交前请检查证据中的敏感信息。</p>
      <Input aria-label="复核人用户名" placeholder="同工作区成员用户名" value={recipient} maxLength={64} onChange={e=>setRecipient(e.target.value)}/>
      <Input.TextArea className="section-gap" aria-label="交接备注" placeholder="请对方核对什么" value={note} maxLength={2000} rows={4}
        onChange={e=>setNote(e.target.value)}/>
    </Modal>
  </section>;
}

export function ReceivedHandoffs() {
  const {message} = App.useApp();
  const [open,setOpen] = useState(false), [busy,setBusy] = useState(false), [error,setError] = useState('');
  const [items,setItems] = useState<ReceivedHandoff[]>([]);
  const [selected,setSelected] = useState<ReceivedHandoff>(), [note,setNote] = useState('');
  async function load() {
    setBusy(true);setError('');
    try {setItems((await api<{items:ReceivedHandoff[]}>('/api/maintenance/handoffs')).items);}
    catch(error) {setError((error as Error).message);} finally {setBusy(false);}
  }
  async function review(status:'reviewed'|'changes_requested') {
    if(!selected||!note.trim()) return;
    setBusy(true);
    try {
      await post('/api/maintenance/handoffs/'+selected.case_id+'/review',{version:selected.version,status,note});
      setSelected(undefined);setNote('');await load();message.success('已提交复核意见');
    } catch(error) {message.error((error as Error).message);await load();} finally {setBusy(false);}
  }
  return <>
    <Button onClick={()=>{setOpen(true);setSelected(undefined);void load();}}>收到的交接</Button>
    <Drawer open={open} onClose={()=>setOpen(false)} title="收到的交接" size="large">
      {error&&<Alert type="error" title={error} action={<Button onClick={()=>void load()}>重试</Button>}/>}
      {selected ? <div className="page-stack">
        <Button onClick={()=>setSelected(undefined)}>返回交接列表</Button>
        <h3>{selected.snapshot.title}</h3>
        <span>{selected.sender} · {formatTime(selected.shared_at)} · {labels[selected.status]}</span>
        <p>{selected.note}</p>
        <p>{selected.snapshot.problem?.symptom}</p>
        <p>{selected.snapshot.summary}</p>
        <p>{selected.snapshot.verification.reason}</p>
        <Collapse items={selected.snapshot.evidence.map(item=>({key:item.id,label:item.label,
          children:<pre className="evidence-lines">{item.text}</pre>}))}/>
        {selected.status==='pending'?<>
          <Input.TextArea aria-label="复核意见" value={note} onChange={e=>setNote(e.target.value)} maxLength={2000} rows={3}/>
          <Space><Button type="primary" loading={busy} disabled={!note.trim()} onClick={()=>void review('reviewed')}>提交复核</Button>
            <Button disabled={busy||!note.trim()} onClick={()=>void review('changes_requested')}>请补充证据</Button></Space>
        </>:<p>{selected.review_note}</p>}
      </div> : <div className="page-stack">
        {busy?'正在加载交接':!items.length&&!error?'暂无交接记录':items.map(item=><div key={item.case_id}>
          <Button type="link" onClick={()=>{setSelected(item);setNote('');}}>{item.snapshot.title}</Button><Tag>{labels[item.status]}</Tag>
        </div>)}
      </div>}
    </Drawer>
  </>;
}
