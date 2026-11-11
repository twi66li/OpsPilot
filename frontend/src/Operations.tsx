import { useCallback, useEffect, useState } from 'react';
import { Alert, App, Button, Card, Collapse, Drawer, Empty, Input, Space, Table, Tag } from 'antd';
import { api, post } from './api';
import type { Approval, Remediation, Ticket } from './types';

const repairStatuses: Record<string, string> = {pending: '待审批', running: '执行中', succeeded: '验证通过', failed: '执行或验证失败', rejected: '已拒绝', interrupted: '执行中断，待核对'};

export default function Operations({canApprove}: {canApprove: boolean}) {
  const {message} = App.useApp();
  const [tickets, setTickets] = useState<Ticket[]>([]), [approvals, setApprovals] = useState<Approval[]>([]);
  const [repairs, setRepairs] = useState<Remediation[]>([]);
  const [detail, setDetail] = useState<Ticket | null>(null), [busy, setBusy] = useState(false), [comment, setComment] = useState('');
  const refresh = useCallback(async () => {
    const [items, pending, remediation] = await Promise.all([api<Ticket[]>('/api/tickets'), canApprove ? api<Approval[]>('/api/agent/approvals') : Promise.resolve([]), api<Remediation[]>('/api/remediations')]);
    setTickets(items); setApprovals(pending); setRepairs(remediation);
  }, [canApprove]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  async function decide(id: string, approved: boolean) {
    setBusy(true);
    try {await post(`/api/agent/approvals/${id}/decide`, {approved, comment}); setComment(''); await refresh(); message.success('审批决定已保存');}
    catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  async function decideRepair(id: string, approved: boolean) {
    setBusy(true);
    try {await post(`/api/remediations/${id}/decide`, {approved, comment}); setComment(''); await refresh(); message.success('执行状态与验证结果已保存');}
    catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="page-stack"><div className="page-title"><div><h2>审批与工单</h2><p>核对调查结论和处理建议，再决定是否进入跟进流程。</p></div><Button onClick={() => refresh().catch(e => message.error(e.message))}>刷新</Button></div>
    {canApprove && <Card title={`待审批 · ${approvals.length}`}><Input.TextArea aria-label="审批意见" value={comment} onChange={e => setComment(e.target.value)} placeholder="工单或演练修复的审批意见（可选）" className="section-bottom" />
      {approvals.length ? <Table<Approval> rowKey="approval_id" dataSource={approvals} columns={[
        {title: '工单草稿', render: (_, item) => <><strong>{String(item.ticket_draft.title || '工单申请')}</strong><pre>{JSON.stringify(item.ticket_draft, null, 2)}</pre></>},
        {title: '操作', width: 200, render: (_, item) => <Space><Button type="primary" loading={busy} onClick={() => decide(item.approval_id, true)}>批准</Button><Button disabled={busy} onClick={() => decide(item.approval_id, false)}>拒绝</Button></Space>},
      ]} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待审批工单" />}</Card>}
    <Card title={`演练修复 · ${repairs.length}`}><Alert type="info" title="仅执行预先定义的测试动作；探针通过表示当前样本恢复，不代表生产根因已修复或长期SLO达标。" className="section-bottom" />
      <Table<Remediation> rowKey="id" dataSource={repairs} expandable={{expandedRowRender: item => <><p>{item.reason}</p><p className="muted">申请人：{item.requested_by} · 审批人：{item.decided_by || '待审批'}</p><Collapse items={[{key: 'result', label: '执行前后证据与验证范围', children: <pre>{JSON.stringify(item.result, null, 2)}</pre>}]} /></>}} columns={[
        {title: '动作', dataIndex: 'action_label'},
        {title: '状态', dataIndex: 'status', render: value => <Tag color={value === 'succeeded' ? 'success' : ['failed', 'interrupted'].includes(value) ? 'error' : 'default'}>{repairStatuses[value] || value}</Tag>},
        {title: '操作', render: (_, item) => item.status === 'pending' && canApprove ? <Space><Button type="primary" loading={busy} onClick={() => decideRepair(item.id, true)}>批准并执行</Button><Button disabled={busy} onClick={() => decideRepair(item.id, false)}>拒绝</Button></Space> : <span className="muted">{item.decided_by || '—'}</span>},
      ]} />
    </Card>
    <Card title={`工单记录 · ${tickets.length}`}><Table<Ticket> rowKey="id" dataSource={tickets} columns={[
      {title: '标题', dataIndex: 'title', render: (title, item) => <Button type="link" onClick={() => api<Ticket>(`/api/tickets/${item.id}`).then(setDetail).catch(e => message.error(e.message))}>{title}</Button>},
      {title: '级别', dataIndex: 'severity', render: value => <Tag color={value === 'P1' ? 'error' : 'warning'}>{value}</Tag>},
      {title: '状态', dataIndex: 'status', render: value => <Tag>{value}</Tag>},
      {title: '创建时间', dataIndex: 'created_at', render: value => new Date(value).toLocaleString()},
    ]} /></Card>
    <Drawer open={!!detail} title={detail?.title} size="large" onClose={() => setDetail(null)}><pre className="source-text">{detail?.description}</pre></Drawer>
  </div>;
}
