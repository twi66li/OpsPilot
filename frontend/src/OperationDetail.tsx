import { useCallback, useEffect, useState } from 'react';
import { Alert, App, Button, Card, Collapse, Descriptions, Drawer, Empty, Input, Popconfirm, Space, Tag, Timeline } from 'antd';
import { api, formatTime, post } from './api';
import type { Run } from './types';

type Detail={kind:string;record:Record<string,unknown>;run:Run|null;impact:string;verification_scope:string};
const statusNames:Record<string,string>={pending:'待审批',approved:'已批准',rejected:'已拒绝',cancelled:'已取消',running:'执行中',succeeded:'样本验证通过',failed:'执行或验证失败',interrupted:'执行中断，需核对',open:'待跟进'};
export default function OperationDetail({id,close,changed}:{id:string;close:()=>void;changed:()=>void}){
 const {message}=App.useApp(),[detail,setDetail]=useState<Detail|null>(null),[error,setError]=useState(''),[comment,setComment]=useState(''),[busy,setBusy]=useState(false);
 const load=useCallback(async()=>{setError('');try{setDetail(await api<Detail>('/api/maintenance/operations/'+id));}catch(e){setError((e as Error).message);}},[id]);
 useEffect(()=>{setDetail(null);void load();},[load]);
 async function decide(approved:boolean){if(!detail)return;setBusy(true);try{await post(detail.kind==='remediation'?`/api/remediations/${id}/decide`:`/api/agent/approvals/${id}/decide`,{approved,comment});await load();changed();message.success('审批决定已保存');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}
 const r=detail?.record,result=detail?.run?.data.result;
 return <Drawer open title="审批、证据与执行详情" size="large" onClose={close}>{error&&<Alert type="error" title={error}/>} {!detail&&!error&&<Empty description="正在读取详情"/>}{detail&&r&&<div className="page-stack"><Tag>{statusNames[String(r.status)]||String(r.status)}</Tag><Descriptions column={1} items={[{key:'action',label:'申请动作',children:detail.kind==='remediation'?'关闭演练故障注入':detail.kind==='ticket_approval'?'创建跟进工单':'查看跟进工单'},{key:'requester',label:'申请人',children:String(r.requested_by||r.created_by||'未记录')},{key:'time',label:'申请时间',children:r.created_at?formatTime(String(r.created_at)):'未记录'},{key:'approver',label:'处理人',children:String(r.decided_by||r.approver||'待处理')}]} />
 <Alert type="warning" title="影响范围" description={detail.impact}/><Card size="small" title="问题与申请理由"><p>{String(r.reason||r.description||(r.ticket_draft as Record<string,unknown>)?.description||'见调查结论')}</p><div className="message-body">{result?.answer||'没有关联的调查回答，请核对原始记录。'}</div></Card>
 <Card size="small" title="执行与验证"><p>{detail.verification_scope}</p><p>{String(r.comment||'尚无审批意见')}</p><Collapse items={[{key:'verification',label:'执行前后请求证据',children:<pre>{JSON.stringify(r.result||{},null,2)}</pre>},{key:'citations',label:'调查引用资料',children:<pre>{JSON.stringify(result?.hits||[],null,2)}</pre>}]} /></Card>
 <Timeline items={[{content:`${r.created_at?formatTime(String(r.created_at)):''} · 提交申请`},...(r.decided_at?[{content:`${formatTime(String(r.decided_at))} · ${String(r.decided_by||r.approver||'')} · ${statusNames[String(r.status)]||String(r.status)}`}]:[])]}/>
 {r.status==='pending'&&detail.kind!=='ticket'&&<><Input.TextArea aria-label="本次审批意见" value={comment} onChange={e=>setComment(e.target.value)} maxLength={600} placeholder="核对证据、影响和风险后填写审批意见"/><Space><Popconfirm title={detail.kind==='remediation'?'确认执行固定测试恢复动作？':'确认创建跟进工单？'} description={detail.impact} onConfirm={()=>decide(true)} okText="确认批准" cancelText="取消"><Button type="primary" loading={busy}>{detail.kind==='remediation'?'批准并执行':'批准创建工单'}</Button></Popconfirm><Button disabled={busy} onClick={()=>decide(false)}>拒绝</Button></Space></>}
 </div>}</Drawer>;
}
