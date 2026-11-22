import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Collapse, Descriptions, Empty, Input, Modal, Pagination, Space, Tag, Timeline } from 'antd';
import { savedAuth, api, followChat, formatTime, post, streamChat } from './api';
import ConversationList from './ConversationList';
import DrillWalkthrough from './DrillWalkthrough';
import StatusTag from './StatusTag';
import type { AgentEvent, Approval, ChatMessage, Hit, Remediation, Run, Session, SessionHistory, PageData } from './types';

const labels: Record<string, string> = {route_intent: '识别调查目标', retrieve: '检索运维手册', grade: '核对证据', rewrite: '补充检索',
  call_tools: '查询观测数据', generate: '整理调查结论', native_tools: '收集证据与分析', prepare_approval: '准备审批草稿',
  approval_gate: '等待人工审批', create_ticket: '创建工单', search_runbook: '检索运维手册', query_service_metrics: '查询服务指标',
  query_logs: '查询服务日志', query_fault_history: '查询历史故障', delegate_retrieval: '委派手册检索'};

function toolHits(event: AgentEvent): Hit[] {
  if (event.agent === 'retrieval' || !['search_runbook', 'delegate_retrieval'].includes(event.name || '')) return [];
  return Array.isArray(event.result?.hits) ? event.result.hits as Hit[] : [];
}
const mergeHits = (previous: Hit[], next: Hit[]) => [...new Map([...previous, ...next].map(hit => [hit.chunk_id, hit])).values()];

export default function Investigation({onSource, canApprove, initialId, initialRunId, opened}: {initialId?:string; initialRunId?:string; opened?:(id:string)=>void; onSource: (hit: Hit) => void; canApprove: boolean}) {
  const guest=!!savedAuth()?.is_guest;
  const {message} = App.useApp();
  const [sessions, setSessions] = useState<Session[]>([]), [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]), [question, setQuestion] = useState(''), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]), [hits, setHits] = useState<Hit[]>([]);
  const [approval, setApproval] = useState<Approval | null>(null), [deciding, setDeciding] = useState(false), [comment, setComment] = useState('');
  const [runId, setRunId] = useState(''), [trace, setTrace] = useState<Run | null>(null);
  const [repairEnabled, setRepairEnabled] = useState(false), [repairOpen, setRepairOpen] = useState(false);
  const [drillService, setDrillService] = useState('');
  const [cancelling, setCancelling] = useState(false);
  const [repairReason, setRepairReason] = useState(''), [repairBusy, setRepairBusy] = useState(false);
  const controller = useRef<AbortController | null>(null), bottom = useRef<HTMLDivElement>(null);
  const [page,setPage]=useState(1),[total,setTotal]=useState(0),[search,setSearch]=useState('');
  const refresh=useCallback(async()=>{const r=await api<PageData<Session>>(`/api/workspace/sessions?page=${page}&page_size=10&q=${encodeURIComponent(search)}`);setSessions(r.items);setTotal(r.total);},[page,search]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    api<{enabled: boolean; service?: string}>('/api/remediations/actions').then(result => {
      setDrillService(result.service || '');
      setRepairEnabled(result.enabled && !!result.service);
    }).catch(() => setRepairEnabled(false));
  }, []);
  useEffect(() => {bottom.current?.scrollIntoView({behavior: 'smooth', block: 'nearest'});}, [messages]);
  useEffect(()=>{if(initialId&&initialId!==sessionId)void selectSession(initialId);},[initialId]);
  function fresh() {setSessionId(''); setMessages([]); setHits([]); setEvents([]); setApproval(null); setTrace(null); setRunId('');setCancelling(false);opened?.('');}
  function receiveRunEvent(event: AgentEvent, answerIndex: number) {
    if (event.run_id) setRunId(event.run_id);
    if (['tool', 'node_status', 'tool_start', 'tool_end', 'error', 'reconnecting', 'connected', 'resumed'].includes(event.type)) {
      setEvents(previous => [...previous, {...event, message: event.type === 'reconnecting' ? '正在重新连接后台调查' : event.message}]);
    }
    if (event.type === 'token') setMessages(previous => previous.map((item, i) => i === answerIndex ? {...item, content: item.content + (event.delta || '')} : item));
    if (event.type === 'answer_reset') setMessages(previous => previous.map((item, i) => i === answerIndex ? {...item, content: ''} : item));
    if (event.type === 'answer' || event.type === 'final') {
      if (event.answer !== undefined) setMessages(previous => previous.map((item, i) => i === answerIndex ? {...item, content: event.answer!, meta: {run_id: event.run_id}} : item));
      if (event.hits) setHits(mergeHits([], event.hits));
      if (event.approval) setApproval(event.approval);
    }
    if (event.type === 'retrieval' && event.hits) setHits(event.hits);
    if (event.type === 'tool') setHits(previous => mergeHits(previous, toolHits(event)));
    if (event.type === 'approval_required' && event.approval_id) setApproval({approval_id: event.approval_id, ticket_draft: event.ticket_draft || {}});
    if (event.type === 'error') message.error(event.message || '调查失败，请查看执行记录');
  }
  async function selectSession(id: string) {
    controller.current?.abort();
    const subscription = new AbortController();
    controller.current = subscription;
    const options = {signal:subscription.signal};
    setBusy(true);setTrace(null);setEvents([]);setHits([]);setRunId('');setApproval(null);
    try {
      const [session, pending] = await Promise.all([
        api<SessionHistory>('/api/sessions/' + id, options),
        api<Approval>('/api/agent/sessions/' + id + '/pending-approval', options),
      ]);
      if(subscription.signal.aborted) return;
      setSessionId(id);opened?.(id);setMessages(session.messages);setApproval(pending.pending ? pending : null);
      const latestRun = initialRunId || session.active_run_id || [...session.messages].reverse().find(item => item.meta?.run_id)?.meta?.run_id;
      if (!latestRun) return;
      setRunId(latestRun);
      if(latestRun===session.active_run_id) {
        const answerIndex = session.messages.findIndex(item => item.role==='assistant' && item.meta?.run_id===latestRun);
        if(answerIndex<0) setMessages(previous => [...previous,{role:'assistant',content:''}]);
        await followChat(latestRun, event => receiveRunEvent(event, answerIndex<0 ? session.messages.length : answerIndex), subscription.signal);
        const saved = await api<SessionHistory>('/api/sessions/' + id, options);
        setMessages(saved.messages);
      }
      const savedRun = await api<Run>('/api/agent/runs/' + latestRun, options);
      setTrace(savedRun);
      const recovered = savedRun.data.result?.hits || savedRun.data.events.filter(item => item.kind === 'tool').flatMap(item => toolHits(item.detail as AgentEvent));
      setHits(mergeHits([], recovered));
      setEvents(savedRun.data.events.filter(item => ['progress', 'tool'].includes(item.kind)).map(item => ({
        ...item.detail, type:item.kind==='tool' ? 'tool' : (item.detail as AgentEvent).type,
      }) as AgentEvent));
    } catch (error) {
      if(!subscription.signal.aborted) message.error((error as Error).message);
    } finally {if(controller.current===subscription) setBusy(false);}
  }
  async function send() {
    const text = question.trim(); if (!text || busy || guest) return;
    setBusy(true); setQuestion(''); setEvents([]); setHits([]); setTrace(null); setRunId('');
    const subscription = new AbortController();
    controller.current = subscription;
    let answerIndex = 0, receivedRunId = '';
    try {
      let id = sessionId;
      if (!id) {
        const created = await post<Session>('/api/sessions', {title: text.slice(0, 40)});
        id = created.id; setSessionId(id); opened?.(id); await refresh();
      }
      answerIndex = messages.length + 1;
      setMessages(previous => [...previous, {role: 'user', content: text}, {role: 'assistant', content: ''}]);
      await streamChat(id, text, event => {
        if (event.run_id) receivedRunId=event.run_id;
        receiveRunEvent(event, answerIndex);
      }, controller.current.signal);
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        const detail = (error as Error).message; message.error(detail);
        setEvents(previous => [...previous, {type: 'error', message: detail}]);
      }
    } finally {
      if(controller.current===subscription) {
        setBusy(false);
        setCancelling(false);
        if(receivedRunId && !subscription.signal.aborted)
          api<Run>('/api/agent/runs/' + receivedRunId, {signal:subscription.signal}).then(setTrace).catch(e=>{if(!subscription.signal.aborted) message.error(e.message);});
      }
    }
  }
  async function stop() {
    if (!runId || cancelling) return;
    setCancelling(true);
    try {
      await post('/api/agent/runs/' + runId + '/cancel');
    } catch (error) {
      setCancelling(false);
      message.error((error as Error).message);
    }
  }
  async function decide(approved: boolean) {
    if (!approval) return; setDeciding(true);
    try {
      const result = await post<{ticket_id?: string}>(`/api/agent/approvals/${approval.approval_id}/decide`, {approved, comment});
      setApproval(null); setComment(''); message.success(result.ticket_id ? `已创建工单 ${result.ticket_id.slice(0, 8)}` : '审批决定已保存');
    } catch (error) {message.error((error as Error).message);} finally {setDeciding(false);}
  }
  async function requestRepair() {
    if (!runId || !repairReason.trim()) return;
    setRepairBusy(true);
    try {
      const result = await post<Remediation>('/api/remediations', {run_id: runId, action: 'reset_demo_fault', reason: repairReason.trim()});
      setRepairOpen(false);
      message.success(result.status === 'pending' ? '修复申请已保存，请到审批与工单页面审批' : '本次调查已有修复记录，请到审批与工单页面查看');
    } catch (error) {message.error((error as Error).message);} finally {setRepairBusy(false);}
  }
  const lastEvent=events.at(-1), draft=approval?.ticket_draft;
  const elapsed=trace?.created_at&&trace.updated_at?(Date.parse(trace.updated_at)-Date.parse(trace.created_at))/1000:null;
  function retry(){setQuestion(trace?.data.question||[...messages].reverse().find(item=>item.role==='user')?.content||'');}
  return <div className="demo-workspace">
    <DrillWalkthrough guest={guest} hasActiveChat={messages.length > 0} />
    <div className="investigation-grid">
    <aside className="session-panel"><div className="panel-heading"><strong>调查会话</strong><Button size="small" disabled={guest||busy} onClick={fresh}>新建</Button></div>
      <Input.Search aria-label="搜索演练会话" placeholder="搜索历史会话" onSearch={v=>{setSearch(v);setPage(1);}} />
      <ConversationList items={sessions.map(s=>({id:s.id,title:s.title,detail:formatTime(s.created_at)}))} current={sessionId} busy={busy} open={id=>void selectSession(id)} deleted={async ids=>{if(ids.includes(sessionId))fresh();await refresh();}} />
      {total>10&&<Pagination simple current={page} total={total} pageSize={10} onChange={setPage} />}
    </aside>
    <section className="chat-panel"><div className="panel-heading"><strong>故障调查</strong><Tag color={busy ? 'processing' : 'default'}>{cancelling ? '已请求停止' : busy ? '正在调查' : '就绪'}</Tag></div>
      <div className="chat-history">{!messages.length && <div className="chat-empty"><h2>体验一次故障调查与恢复</h2><p>测试环境会模拟连接池等待超时，可能看到请求失败、延迟上升和 ERR_5003 日志。先调查证据，再申请关闭故障注入，经审批后观察请求验证结果；实际异常以工具查询为准。</p>
        {guest ? <Alert type="info" title="访客模式仅支持只读浏览，退出访客并登录个人账号后即可发起调查演练。" /> : <Button disabled={!drillService} onClick={() => setQuestion(`调查 ${drillService} 最近5分钟的错误，结合日志、指标和手册，说明已确认事实与证据缺口。`)}>填入调查示例</Button>}</div>}
        {messages.map((item, i) => <article className={`chat-message ${item.role}`} key={i}><div className="message-label">{item.role === 'user' ? '你' : 'OpsPilot'}</div><div className="message-body">{item.content || (busy ? '正在收集证据…' : '本次没有生成回答，请查看执行记录。')}</div></article>)}
        {approval && <Card className="approval-card" title="工单等待审批" size="small"><Descriptions column={1} size="small" items={['title','service','severity','description'].map((key,index)=>({key,label:['标题','服务','严重度','说明'][index],children:String(draft?.[key]||'未提供')}))}/><Collapse size="small" ghost items={[{key:'draft',label:'查看工单原始数据',children:<pre>{JSON.stringify(draft,null,2)}</pre>}]}/><Input.TextArea aria-label="审批意见" value={comment} onChange={e => setComment(e.target.value)} placeholder="审批意见" />
          <Space className="section-gap"><Button type="primary" disabled={!canApprove} loading={deciding} onClick={() => decide(true)}>批准创建工单</Button><Button disabled={!canApprove} loading={deciding} onClick={() => decide(false)}>拒绝</Button></Space>
        </Card>}<div ref={bottom} />
      </div>
      <div className="composer"><Input.TextArea aria-label="调查问题" disabled={guest} value={question} onChange={e => setQuestion(e.target.value)} autoSize={{minRows: 2, maxRows: 5}} placeholder="服务名、异常表现、时间范围，以及需要核对的证据…" onKeyDown={e => {if (e.ctrlKey && e.key === 'Enter') void send();}} />
        <div className="composer-footer"><span>Ctrl + Enter 发送 · 手册经验需结合当前证据判断</span>{busy ? <Button danger disabled={!runId || cancelling} onClick={stop}>停止调查</Button> : <Button type="primary" disabled={guest||!question.trim() || !!approval} onClick={send}>开始调查</Button>}</div>
      </div>
    </section>
    <aside className="evidence-panel"><div className="panel-heading"><strong>证据与执行过程</strong></div><div className="evidence-scroll">
      {(busy||trace||lastEvent)&&<section className="investigation-summary" aria-label="本次调查摘要"><h4>本次调查</h4>{trace&&<StatusTag status={trace.status}/>}<p>{busy?'正在收集证据与整理结论':trace?.data.result?.message||trace?.data.result?.end_reason==='answered'&&'已保存本次调查回答；请结合证据核对。'||'按保存的执行记录查看进展。'}</p>{lastEvent&&<p>最近阶段：{labels[lastEvent.name||lastEvent.node||'']||'调查进展'} · {lastEvent.summary||lastEvent.message||'已记录'}</p>}{elapsed!==null&&Number.isFinite(elapsed)&&elapsed>=0&&<p className="muted">从开始至最后记录更新 {elapsed.toFixed(1)} 秒 · {formatTime(trace!.updated_at!)}</p>}
        {!busy&&trace&&['interrupted','failed','cancelled'].includes(trace.status)&&<><p>原问题与已有记录保留，重新调查将创建一次新运行。</p><Button disabled={guest||!!approval} onClick={retry}>用已保存问题重新调查</Button></>}
      </section>}
      <h4>知识引用 · {hits.length}</h4>{!hits.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="检索到手册后显示来源" />}
      {hits.map((hit, i) => <button className="evidence-card" key={`${hit.chunk_id}-${i}`} onClick={() => onSource(hit)}><strong>[{hit.citation_id || i + 1}] {hit.doc_title}</strong><small>{hit.version ? `v${hit.version} · ` : ''}{hit.page_start ? `第${hit.page_start}页` : '文本来源'}</small><p>{hit.text.slice(0, 160)}</p><span>查看原文与上下文 →</span></button>)}
      <Collapse className="section-bottom" size="small" items={[{key:'process',label:`执行过程 · ${events.length} 条记录${lastEvent?' · '+(labels[lastEvent.name||lastEvent.node||'']||'调查进展'):''}`,children:<Timeline items={events.map((event, i) => ({key: i, color: event.type === 'error' ? 'red' : 'blue', content: <><strong>{labels[event.name || event.node || ''] || (event.type === 'error' ? '执行异常' : '调查进展')}</strong><p className="muted">{event.summary || event.message || (event.type === 'tool_start' ? '正在查询…' : '已记录')}</p><Collapse size="small" ghost items={[{key: 'raw', label: '查看记录', children: <pre>{JSON.stringify(event, null, 2)}</pre>}]} /></>}))}/>}]}/>
      {runId && <><small className="muted">Run {runId.slice(0, 12)}</small><Button size="small" onClick={() => api<Run>(`/api/agent/runs/${runId}`).then(setTrace).catch(e => message.error(e.message))}>读取完整记录</Button></>}
      {runId && repairEnabled && canApprove && <Button className="section-gap" disabled={busy} onClick={() => {setRepairReason(`根据本次调查，申请关闭 ${drillService} 的演练故障注入，并核对实际请求结果。`); setRepairOpen(true);}}>申请演练修复</Button>}
      {trace && <Collapse size="small" items={[{key: 'trace', label: '查看调查原始数据', children: <pre>{JSON.stringify(trace.data, null, 2)}</pre>}]} />}
      {approval && !canApprove && <Alert type="info" title="当前账号可查看，需要工程师或管理员审批。" />}
    </div></aside>
    <Modal open={repairOpen} title="申请演练修复" onCancel={() => setRepairOpen(false)} onOk={requestRepair} confirmLoading={repairBusy} okText="提交审批" okButtonProps={{disabled: !repairReason.trim()}}>
      <Alert type="warning" title={`将关闭 ${drillService} 的测试故障注入。批准后使用短请求探针验证。`} />
      <Input.TextArea className="section-gap" aria-label="演练修复理由" value={repairReason} maxLength={600} rows={4} onChange={e => setRepairReason(e.target.value)} />
    </Modal>
  </div></div>;
}
