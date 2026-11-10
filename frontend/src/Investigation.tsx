import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Collapse, Empty, Input, Space, Tag, Timeline } from 'antd';
import { api, post, streamChat } from './api';
import type { AgentEvent, Approval, ChatMessage, Hit, Run, Session } from './types';

const labels: Record<string, string> = {route_intent: '识别调查目标', retrieve: '检索运维手册', grade: '核对证据', rewrite: '补充检索',
  call_tools: '查询观测数据', generate: '整理调查结论', native_tools: '收集证据与分析', prepare_approval: '准备审批草稿',
  approval_gate: '等待人工审批', create_ticket: '创建工单', search_runbook: '检索运维手册', query_service_metrics: '查询服务指标',
  query_logs: '查询服务日志', query_fault_history: '查询历史故障', delegate_retrieval: '委派手册检索'};

export default function Investigation({onSource, canApprove}: {onSource: (hit: Hit) => void; canApprove: boolean}) {
  const {message} = App.useApp();
  const [sessions, setSessions] = useState<Session[]>([]), [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]), [question, setQuestion] = useState(''), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]), [hits, setHits] = useState<Hit[]>([]);
  const [approval, setApproval] = useState<Approval | null>(null), [deciding, setDeciding] = useState(false), [comment, setComment] = useState('');
  const [runId, setRunId] = useState(''), [trace, setTrace] = useState<Run | null>(null);
  const controller = useRef<AbortController | null>(null), bottom = useRef<HTMLDivElement>(null);
  const refresh = useCallback(async () => setSessions(await api<Session[]>('/api/sessions')), []);
  useEffect(() => {refresh().catch(e => message.error(e.message)); return () => controller.current?.abort();}, [refresh, message]);
  useEffect(() => {bottom.current?.scrollIntoView({behavior: 'smooth', block: 'nearest'});}, [messages]);
  async function selectSession(id: string) {
    setBusy(true); setTrace(null); setEvents([]); setHits([]); setRunId(''); setApproval(null);
    try {
      const [session, pending] = await Promise.all([
        api<Session & {messages: ChatMessage[]}>(`/api/sessions/${id}`), api<Approval>(`/api/agent/sessions/${id}/pending-approval`),
      ]);
      setSessionId(id); setMessages(session.messages); setApproval(pending.pending ? pending : null);
      const latestRun = [...session.messages].reverse().find(item => item.meta?.run_id)?.meta?.run_id;
      if (latestRun) {
        setRunId(latestRun);
        const run = await api<Run>(`/api/agent/runs/${latestRun}`); setTrace(run);
        const final = run.data.result?.hits ? run.data.result : [...run.data.events].reverse().map(item => item.detail as AgentEvent).find(item => item.type === 'final');
        setHits(final?.hits || []);
        setEvents(run.data.events.filter(item => item.kind === 'sse').map(item => item.detail as AgentEvent).filter(item => ['tool', 'node_status', 'tool_start'].includes(item.type)));
      }
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  async function send() {
    const text = question.trim(); if (!text || busy) return;
    setBusy(true); setQuestion(''); setEvents([]); setHits([]); setTrace(null); setRunId('');
    controller.current = new AbortController();
    let answerIndex = 0;
    try {
      let id = sessionId;
      if (!id) {
        const created = await post<Session>('/api/sessions', {title: text.slice(0, 40)});
        id = created.id; setSessionId(id); await refresh();
      }
      answerIndex = messages.length + 1;
      setMessages(previous => [...previous, {role: 'user', content: text}, {role: 'assistant', content: ''}]);
      await streamChat(id, text, event => {
        if (event.run_id) setRunId(event.run_id);
        if (['tool', 'node_status', 'tool_start', 'tool_end', 'error'].includes(event.type)) setEvents(previous => [...previous, event]);
        if (event.type === 'token') setMessages(previous => previous.map((item, i) => i === answerIndex ? {...item, content: item.content + (event.delta || '')} : item));
        if (event.type === 'answer' || event.type === 'final') {
          if (event.answer !== undefined) setMessages(previous => previous.map((item, i) => i === answerIndex ? {...item, content: event.answer!, meta: {run_id: event.run_id}} : item));
          if (event.hits) setHits(event.hits);
          if (event.approval) setApproval(event.approval);
        }
        if (event.type === 'retrieval' && event.hits) setHits(event.hits);
        if (event.type === 'approval_required' && event.approval_id) setApproval({approval_id: event.approval_id, ticket_draft: event.ticket_draft || {}});
        if (event.type === 'error') message.error(event.message || '调查失败，请查看执行记录');
      }, controller.current.signal);
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        const detail = (error as Error).message; message.error(detail);
        setEvents(previous => [...previous, {type: 'error', message: detail}]);
      }
    } finally {setBusy(false);}
  }
  async function decide(approved: boolean) {
    if (!approval) return; setDeciding(true);
    try {
      const result = await post<{ticket_id?: string}>(`/api/agent/approvals/${approval.approval_id}/decide`, {approved, comment});
      setApproval(null); setComment(''); message.success(result.ticket_id ? `已创建工单 ${result.ticket_id.slice(0, 8)}` : '审批决定已保存');
    } catch (error) {message.error((error as Error).message);} finally {setDeciding(false);}
  }
  return <div className="investigation-grid">
    <aside className="session-panel"><div className="panel-heading"><strong>调查会话</strong><Button size="small" disabled={busy} onClick={() => {setSessionId(''); setMessages([]); setHits([]); setEvents([]); setApproval(null); setTrace(null);}}>新建</Button></div>
      {sessions.map(session => <button disabled={busy} key={session.id} className={`session-item ${session.id === sessionId ? 'selected' : ''}`} onClick={() => selectSession(session.id)}>{session.title}<small>{new Date(session.created_at).toLocaleString()}</small></button>)}
    </aside>
    <section className="chat-panel"><div className="panel-heading"><strong>故障调查</strong><Tag color={busy ? 'processing' : 'default'}>{busy ? '正在调查' : '就绪'}</Tag></div>
      <div className="chat-history">{!messages.length && <div className="chat-empty"><span className="eyebrow">从证据开始</span><h2>描述异常，逐步确认原因</h2><p>结合实时日志、指标与运维手册，保留调查过程和引用依据。</p>
        <Button onClick={() => setQuestion('调查 order-service 最近5分钟的错误，结合日志、指标和手册，说明已确认事实与证据缺口。')}>填入调查示例</Button></div>}
        {messages.map((item, i) => <article className={`chat-message ${item.role}`} key={i}><div className="message-label">{item.role === 'user' ? '你' : 'OpsPilot'}</div><div className="message-body">{item.content || (busy ? '正在收集证据…' : '本次没有生成回答，请查看执行记录。')}</div></article>)}
        {approval && <Card className="approval-card" title="工单等待审批" size="small"><pre>{JSON.stringify(approval.ticket_draft, null, 2)}</pre><Input.TextArea aria-label="审批意见" value={comment} onChange={e => setComment(e.target.value)} placeholder="审批意见" />
          <Space className="section-gap"><Button type="primary" disabled={!canApprove} loading={deciding} onClick={() => decide(true)}>批准创建工单</Button><Button disabled={!canApprove} loading={deciding} onClick={() => decide(false)}>拒绝</Button></Space>
        </Card>}<div ref={bottom} />
      </div>
      <div className="composer"><Input.TextArea aria-label="调查问题" value={question} onChange={e => setQuestion(e.target.value)} autoSize={{minRows: 2, maxRows: 5}} placeholder="服务名、异常表现、时间范围，以及需要核对的证据…" onKeyDown={e => {if (e.ctrlKey && e.key === 'Enter') void send();}} />
        <div className="composer-footer"><span>Ctrl + Enter 发送 · 手册经验需结合当前证据判断</span><Button type="primary" loading={busy} disabled={!question.trim() || !!approval} onClick={send}>开始调查</Button></div>
      </div>
    </section>
    <aside className="evidence-panel"><div className="panel-heading"><strong>证据与执行过程</strong></div><div className="evidence-scroll">
      <h4>知识引用 · {hits.length}</h4>{!hits.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="检索到手册后显示来源" />}
      {hits.map((hit, i) => <button className="evidence-card" key={`${hit.chunk_id}-${i}`} onClick={() => onSource(hit)}><strong>[{i + 1}] {hit.doc_title}</strong><small>{hit.version ? `v${hit.version} · ` : ''}{hit.page_start ? `第${hit.page_start}页` : '文本来源'}</small><p>{hit.text.slice(0, 160)}</p><span>查看原文与上下文 →</span></button>)}
      <h4>执行过程</h4><Timeline items={events.map((event, i) => ({key: i, color: event.type === 'error' ? 'red' : 'green', content: <><strong>{labels[event.name || event.node || ''] || (event.type === 'error' ? '执行异常' : '调查进展')}</strong><p className="muted">{event.summary || event.message || (event.type === 'tool_start' ? '正在查询…' : '已完成')}</p><Collapse size="small" ghost items={[{key: 'raw', label: '查看记录', children: <pre>{JSON.stringify(event, null, 2)}</pre>}]} /></>}))} />
      {runId && <><small className="muted">Run {runId.slice(0, 12)}</small><Button size="small" onClick={() => api<Run>(`/api/agent/runs/${runId}`).then(setTrace).catch(e => message.error(e.message))}>读取完整记录</Button></>}
      {trace && <Collapse size="small" items={[{key: 'trace', label: `调查记录 · ${trace.status}`, children: <pre>{JSON.stringify(trace.data, null, 2)}</pre>}]} />}
      {approval && !canApprove && <Alert type="info" title="当前账号可查看，需要工程师或管理员审批。" />}
    </div></aside>
  </div>;
}
