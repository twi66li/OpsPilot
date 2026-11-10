import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Drawer, Empty, Input, Space, Table, Tag } from 'antd';
import { api, post } from './api';
import type { Approval, Ticket } from './types';

export default function Operations({canApprove}: {canApprove: boolean}) {
  const {message} = App.useApp();
  const [tickets, setTickets] = useState<Ticket[]>([]), [approvals, setApprovals] = useState<Approval[]>([]);
  const [detail, setDetail] = useState<Ticket | null>(null), [busy, setBusy] = useState(false), [comment, setComment] = useState('');
  const refresh = useCallback(async () => {
    const [items, pending] = await Promise.all([api<Ticket[]>('/api/tickets'), canApprove ? api<Approval[]>('/api/agent/approvals') : Promise.resolve([])]);
    setTickets(items); setApprovals(pending);
  }, [canApprove]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  async function decide(id: string, approved: boolean) {
    setBusy(true);
    try {await post(`/api/agent/approvals/${id}/decide`, {approved, comment}); setComment(''); await refresh(); message.success('审批决定已保存');}
    catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="page-stack"><div className="page-title"><div><h2>审批与工单</h2><p>核对调查结论和处理建议，再决定是否进入跟进流程。</p></div><Button onClick={() => refresh().catch(e => message.error(e.message))}>刷新</Button></div>
    {canApprove && <Card title={`待审批 · ${approvals.length}`}><Input.TextArea aria-label="工单审批意见" value={comment} onChange={e => setComment(e.target.value)} placeholder="审批意见（可选）" className="section-bottom" />
      {approvals.length ? <Table<Approval> rowKey="approval_id" dataSource={approvals} columns={[
        {title: '工单草稿', render: (_, item) => <><strong>{String(item.ticket_draft.title || '工单申请')}</strong><pre>{JSON.stringify(item.ticket_draft, null, 2)}</pre></>},
        {title: '操作', width: 200, render: (_, item) => <Space><Button type="primary" loading={busy} onClick={() => decide(item.approval_id, true)}>批准</Button><Button disabled={busy} onClick={() => decide(item.approval_id, false)}>拒绝</Button></Space>},
      ]} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待审批工单" />}</Card>}
    <Card title={`工单记录 · ${tickets.length}`}><Table<Ticket> rowKey="id" dataSource={tickets} columns={[
      {title: '标题', dataIndex: 'title', render: (title, item) => <Button type="link" onClick={() => api<Ticket>(`/api/tickets/${item.id}`).then(setDetail).catch(e => message.error(e.message))}>{title}</Button>},
      {title: '级别', dataIndex: 'severity', render: value => <Tag color={value === 'P1' ? 'error' : 'warning'}>{value}</Tag>},
      {title: '状态', dataIndex: 'status', render: value => <Tag>{value}</Tag>},
      {title: '创建时间', dataIndex: 'created_at', render: value => new Date(value).toLocaleString()},
    ]} /></Card>
    <Drawer open={!!detail} title={detail?.title} size="large" onClose={() => setDetail(null)}><pre className="source-text">{detail?.description}</pre></Drawer>
  </div>;
}
