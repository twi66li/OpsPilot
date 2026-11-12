import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Input, Space, Table, Tag } from 'antd';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, formatTime } from './api';
import type { Approval, PageData, Remediation, Ticket } from './types';
import OperationDetail from './OperationDetail';

export default function Operations({canApprove}:{canApprove:boolean}){
 const {message}=App.useApp(),navigate=useNavigate(),location=useLocation(),id=location.pathname.split('/')[2];
 const [approvals,setApprovals]=useState<Approval[]>([]),[repairs,setRepairs]=useState<Remediation[]>([]),[tickets,setTickets]=useState<Ticket[]>([]);
 const [pages,setPages]=useState({approvals:1,remediations:1,tickets:1}),[totals,setTotals]=useState({approvals:0,remediations:0,tickets:0}),[query,setQuery]=useState('');
 const refresh=useCallback(async()=>{if(!canApprove)return;const [a,r,t]=await Promise.all([api<PageData<Approval>>(`/api/workspace/approvals?status=pending&page=${pages.approvals}&page_size=10&q=${encodeURIComponent(query)}`),api<PageData<Remediation>>(`/api/workspace/remediations?page=${pages.remediations}&page_size=10&q=${encodeURIComponent(query)}`),api<PageData<Ticket>>(`/api/workspace/tickets?page=${pages.tickets}&page_size=10&q=${encodeURIComponent(query)}`)]);setApprovals(a.items);setRepairs(r.items);setTickets(t.items);setTotals({approvals:a.total,remediations:r.total,tickets:t.total});},[canApprove,pages,query]);
 useEffect(()=>{void refresh().catch(e=>message.error(e.message));},[refresh,message]);
 const pagination=(kind:keyof typeof pages)=>({current:pages[kind],total:totals[kind],pageSize:10,onChange:(page:number)=>setPages(previous=>({...previous,[kind]:page}))});
 return <div className="page-stack"><div className="page-title"><div><h2>审批与工单</h2><p>打开详情核对证据、动作、影响和验证范围，再决定是否批准。</p></div><Space><Input.Search aria-label="搜索审批与工单" placeholder="搜索标题或申请理由" onSearch={v=>{setQuery(v);setPages({approvals:1,remediations:1,tickets:1});}}/><Button onClick={()=>void refresh().catch(e=>message.error(e.message))}>刷新</Button></Space></div>
 <Card title={`待审批工单 · ${totals.approvals}`}><Table<Approval> rowKey="approval_id" dataSource={approvals} pagination={pagination('approvals')} columns={[{title:'工单草稿',render:(_,item)=>String(item.ticket_draft.title||'工单申请')},{title:'操作',render:(_,item)=><Button onClick={()=>navigate('/operations/'+item.approval_id)}>查看并审批</Button>}]} /></Card>
 <Card title={`演练修复 · ${totals.remediations}`}><Table<Remediation> rowKey="id" dataSource={repairs} pagination={pagination('remediations')} columns={[{title:'固定动作',dataIndex:'action_label'},{title:'状态',dataIndex:'status',render:value=><Tag>{{pending:'待审批',succeeded:'样本验证通过',cancelled:'已取消',rejected:'已拒绝',running:'执行中',failed:'执行或验证失败',interrupted:'执行中断'}[value as string]||value}</Tag>},{title:'操作',render:(_,item)=><Button onClick={()=>navigate('/operations/'+item.id)}>查看证据与审批</Button>}]} /></Card>
 <Card title={`跟进工单 · ${totals.tickets}`}><Table<Ticket> rowKey="id" dataSource={tickets} pagination={pagination('tickets')} columns={[{title:'标题',dataIndex:'title',render:(value,item)=><Button type="link" onClick={()=>navigate('/operations/'+item.id)}>{value}</Button>},{title:'严重度',dataIndex:'severity'},{title:'状态',dataIndex:'status'},{title:'创建时间',dataIndex:'created_at',render:formatTime}]} /></Card>
 {id&&<OperationDetail id={id} close={()=>navigate('/operations')} changed={()=>void refresh().catch(e=>message.error(e.message))}/>}</div>;
}
