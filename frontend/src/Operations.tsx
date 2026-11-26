import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Input, Space, Table, Tag } from 'antd';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, formatTime } from './api';
import type { Approval, PageData, Remediation, Ticket } from './types';
import OperationDetail from './OperationDetail';

const ticketStatus:Record<string,string>={open:'处理中',resolved:'已解决',rejected:'已拒绝'};
const approvalStatus:Record<string,string>={pending:'待审批',approved:'已批准',rejected:'已拒绝'};
export default function Operations({readOnly,drillEnabled}:{readOnly:boolean;drillEnabled:boolean}){
 const {message}=App.useApp(),navigate=useNavigate(),location=useLocation(),id=location.pathname.split('/')[2];
 const [approvals,setApprovals]=useState<Approval[]>([]),[repairs,setRepairs]=useState<Remediation[]>([]),[tickets,setTickets]=useState<Ticket[]>([]);
 const [pages,setPages]=useState({approvals:1,remediations:1,tickets:1}),[totals,setTotals]=useState({approvals:0,remediations:0,tickets:0}),[query,setQuery]=useState('');
 const refresh=useCallback(async()=>{const [a,r,t]=await Promise.all([api<PageData<Approval>>(`/api/workspace/approvals?status=${readOnly?'':'pending'}&page=${pages.approvals}&page_size=10&q=${encodeURIComponent(query)}`),drillEnabled?api<PageData<Remediation>>(`/api/workspace/remediations?page=${pages.remediations}&page_size=10&q=${encodeURIComponent(query)}`):null,api<PageData<Ticket>>(`/api/workspace/tickets?page=${pages.tickets}&page_size=10&q=${encodeURIComponent(query)}`)]);setApprovals(a.items);setRepairs(r?.items||[]);setTickets(t.items);setTotals({approvals:a.total,remediations:r?.total||0,tickets:t.total});},[readOnly,drillEnabled,pages,query]);
 useEffect(()=>{void refresh().catch(e=>message.error(e.message));},[refresh,message]);
 const pagination=(kind:keyof typeof pages)=>totals[kind]?{current:pages[kind],total:totals[kind],pageSize:10,onChange:(page:number)=>setPages(previous=>({...previous,[kind]:page}))}:false;
 return <div className="page-stack"><div className="page-title operations-title"><h2>审批与工单</h2><Space wrap><Input.Search aria-label="搜索审批与工单" placeholder="搜索标题或申请理由" onSearch={v=>{setQuery(v);setPages({approvals:1,remediations:1,tickets:1});}}/><Button onClick={()=>void refresh().catch(e=>message.error(e.message))}>刷新</Button></Space></div>
 <Card size="small" className="ops-table-compact" title={`${readOnly?'示例审批记录':'待审批工单'} · ${totals.approvals}`}><Table<Approval> size="small" rowKey="approval_id" dataSource={approvals} locale={{emptyText:'暂无审批记录'}} pagination={pagination('approvals')} columns={[
   {title:'工单草稿',render:(_,item)=><div>{String(item.ticket_draft.title||'工单申请')}{readOnly&&<div><small className="muted">{item.created_at&&formatTime(item.created_at)}</small></div>}</div>},
   ...(readOnly?[{title:'状态',width:90,render:(_:unknown,item:Approval)=><Tag>{approvalStatus[item.status || ''] || item.status}</Tag>}]:[]),
   {title:'操作',width:readOnly?72:140,render:(_,item)=><Button size="small" onClick={()=>navigate('/operations/'+item.approval_id)}>{readOnly?'查看':'查看并审批'}</Button>}
 ]} /></Card>
 {drillEnabled&&<Card size="small" className="ops-table-compact" title={`演练修复 · ${totals.remediations}`}><Table<Remediation> size="small" rowKey="id" dataSource={repairs} locale={{emptyText:'暂无演练修复记录'}} pagination={pagination('remediations')} columns={[{title:'固定动作',dataIndex:'action_label'},{title:'状态',dataIndex:'status',render:value=><Tag>{{pending:'待审批',succeeded:'样本验证通过',cancelled:'已取消',rejected:'已拒绝',running:'执行中',failed:'执行或验证失败',interrupted:'执行中断'}[value as string]||value}</Tag>},{title:'操作',width:160,render:(_,item)=><Button size="small" onClick={()=>navigate('/operations/'+item.id)}>查看证据与审批</Button>}]} /></Card>}
 <Card size="small" className="ops-table-compact" title={`跟进工单 · ${totals.tickets}`}><Table<Ticket> size="small" rowKey="id" dataSource={tickets} locale={{emptyText:'暂无跟进工单'}} pagination={pagination('tickets')} columns={[
   {title:'标题',dataIndex:'title',render:(value,item)=><div><Button className="operation-ticket-link" type="link" onClick={()=>navigate('/operations/'+item.id)}>{value}</Button>{readOnly&&<div><small className="muted">{formatTime(item.created_at)}</small></div>}</div>},
   {title:'严重度',dataIndex:'severity',width:90,responsive:['sm']},
   {title:'状态',dataIndex:'status',width:90,render:value=><Tag color={value==='resolved'?'success':value==='rejected'?'default':'processing'}>{ticketStatus[value as string]||value}</Tag>},
   ...(!readOnly?[{title:'创建时间',dataIndex:'created_at',width:170,render:formatTime}]:[])
 ]} /></Card>
 {id&&<OperationDetail id={id} readOnly={readOnly} close={()=>navigate('/operations')} changed={()=>void refresh().catch(e=>message.error(e.message))}/>}</div>;
}
