import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Collapse, Drawer, Empty, Input, Modal, Pagination, Select, Skeleton, Space, Tag } from 'antd';
import { ArrowLeftOutlined, HistoryOutlined } from '@ant-design/icons';
import { savedAuth, api, followChat, newRequestId, post, streamChat } from './api';
import type { AgentEvent, ChatMessage, EvidencePreview, Hit, PageData, PersonalCase, SessionHistory, Site, SupportEntry, Workflow } from './types';
import { caseStates as states, verdictLabels as verdicts, workflowNames } from './supportTypes';
import StatusTag from './StatusTag';
import SiteEditor from './SiteEditor';
import CollectionGuide from './CollectionGuide';
import MaintenanceTimeline from './MaintenanceTimeline';
import SampleResults from './SampleResults';
import PlanChecklist from './PlanChecklist';
import ConversationList from './ConversationList';
import ResultCard from './ResultCard';
import ProcessFold from './ProcessFold';
import EvidenceSummary, { RedactionNotice } from './EvidenceSummary';
import { HandoffEditor } from './CaseHandoff';

export default function PersonalSupport({entry, onSource, onKnowledge, onIssues, onChat, opened, issueDetail=false}: {issueDetail?:boolean;opened?:(id:string)=>void; entry?: SupportEntry; onSource: (hit: Hit) => void; onKnowledge: () => void; onIssues: (id?: string) => void; onChat?: () => void}) {
  const {message} = App.useApp();
  const [cases, setCases] = useState<PersonalCase[]>([]), [sites, setSites] = useState<Site[]>([]);
  const [current, setCurrent] = useState<PersonalCase | null>(null), [siteId, setSiteId] = useState<string | undefined>();
  const [caseTitle, setCaseTitle] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]), [question, setQuestion] = useState(''), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]), [hits, setHits] = useState<Hit[]>([]);
  const [siteOpen, setSiteOpen] = useState(false), [editingSite, setEditingSite] = useState<string | undefined>();
  const [workflow, setWorkflow] = useState<Workflow>('auto'), [evidenceEndpoint, setEvidenceEndpoint] = useState(''), [evidenceStep, setEvidenceStep] = useState<number | undefined>();
  const [evidenceOpen, setEvidenceOpen] = useState(false), [evidencePanelOpen, setEvidencePanelOpen] = useState(false), [evidenceText, setEvidenceText] = useState(''), [evidenceLabel, setEvidenceLabel] = useState('应用错误日志');
  const [evidenceFocus,setEvidenceFocus] = useState<{id:string;start:number;end:number;lines?:number[]}|null>(null), [evidenceKeys,setEvidenceKeys] = useState<string[]>([]);
  const [evidencePreview,setEvidencePreview]=useState<EvidencePreview|null>(null);
  const evidenceLine = useRef<HTMLSpanElement|null>(null);
  const [evidenceKind, setEvidenceKind] = useState<'initial' | 'check_result'>('initial'), [collectedAt, setCollectedAt] = useState(''), [previewed, setPreviewed] = useState(false);
  const [actionBusy, setActionBusy] = useState(false), [recordAction, setRecordAction] = useState<'archive'|'resolve'|null>(null), [solution, setSolution] = useState(''), [confirmed, setConfirmed] = useState(false);
  const controller = useRef<AbortController | null>(null), conversation = useRef<HTMLDivElement>(null);
  const request = useRef({id:'',signature:''});
  const [waitingRun,setWaitingRun]=useState(false),[casePage,setCasePage]=useState(1),[caseTotal,setCaseTotal]=useState(0);
  const run = useRef(''), liveWorkflow = useRef('auto'), rawDraft = useRef('');
  const [phase,setPhase] = useState(''), [draft,setDraft] = useState(''), [updated,setUpdated] = useState<number|null>(null), [cancelling,setCancelling] = useState(false);
  const [startedAt,setStartedAt]=useState<number|null>(null),[loadError,setLoadError]=useState(''),[loadingCase,setLoadingCase]=useState(false);
  const [lastQuestion,setLastQuestion] = useState(''), [failed,setFailed] = useState(false);
  const [historyOpen,setHistoryOpen] = useState(false);
  const refresh = useCallback(async () => {
    const [records, profiles] = await Promise.all([api<PageData<PersonalCase>>(`/api/workspace/cases?page=${casePage}&page_size=10`), api<Site[]>('/api/support/sites')]);
    setCases(records.items);setCaseTotal(records.total); setSites(profiles);
  }, [casePage]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {if(entry?.question)request.current={id:'',signature:''};if (entry?.caseId) {if(entry.caseId!==current?.id)void load(entry.caseId);else if(entry.question){setQuestion(entry.question);setWorkflow(entry.workflow||'auto');}} else {fresh(); setSiteId(entry?.siteId); setWorkflow(entry?.workflow || 'auto'); setQuestion(entry?.question || '');}}, [entry]);
  useEffect(()=>{const line=evidenceLine.current,pre=line?.parentElement;if(evidencePanelOpen&&line&&pre){pre.scrollTop=line.offsetTop-24;line.focus({preventScroll:true});}},[evidenceFocus,evidencePanelOpen]);
  useEffect(()=>{if(!cancelling&&!waitingRun)return;const timer=setInterval(()=>{if(run.current)api<{status:string}>(`/api/agent/runs/${run.current}`).then(r=>{if(!['running','cancel_requested'].includes(r.status)){setCancelling(false);setWaitingRun(false);setPhase(['completed','answered'].includes(r.status)?'分析已完成':'运行已结束，已有证据保留');if(current)void load(current.id);}}).catch(()=>{});},1500);return()=>clearInterval(timer);},[cancelling,waitingRun,current?.id]);
  async function stop() {if(!run.current)return;setCancelling(true);setPhase('正在停止，请等待当前请求收尾');try{await post(`/api/agent/runs/${run.current}/cancel`);controller.current?.abort();}catch(e){setCancelling(false);message.error((e as Error).message);}}
  async function load(id: string) {
    controller.current?.abort();
    const subscription = new AbortController();
    controller.current = subscription;
    setEvidencePanelOpen(false);setEvidenceFocus(null);setEvidenceKeys([]);
    setLoadingCase(true);setBusy(true);setCurrent(null);setCaseTitle('');setMessages([]);setHits([]);setLoadError('');setEvents([]);
    setWorkflow('auto');setQuestion('');setDraft('');setPhase('');setFailed(false);
    run.current='';rawDraft.current='';liveWorkflow.current='auto';
    let loaded = false;
    try {
      const [record, history] = await readCase(id, subscription.signal);
      if(subscription.signal.aborted) return;
      setCurrent(record);setCaseTitle(history.title);opened?.(record.id);setSiteId(record.site_id);
      setMessages(history.messages);setHits(record.report_hits || []);
      loaded = true;
      setLoadingCase(false);
      if(entry?.caseId===id&&entry.question){setQuestion(entry.question);setWorkflow(entry.workflow||'auto');}
      if(history.active_run_id) {
        run.current = history.active_run_id;
        setPhase('已连接到后台分析');
        await followChat(history.active_run_id, event => receiveRunEvent(event, 'auto'), subscription.signal);
        await refreshResult(id, subscription.signal);
      }
    } catch (error) {
      if(subscription.signal.aborted) return;
      if(loaded) setPhase('连接中断，重新打开问题可查看后台状态'); else setLoadError((error as Error).message);
      message.error((error as Error).message);
    } finally {if(controller.current===subscription) {setLoadingCase(false);setBusy(false);}}
  }
  function readCase(id: string, signal: AbortSignal) {
    return Promise.all([api<PersonalCase>('/api/support/cases/' + id, {signal}), api<SessionHistory>('/api/sessions/' + id, {signal})]);
  }
  async function refreshResult(id: string, signal: AbortSignal) {
    const [saved, history] = await readCase(id, signal);
    if(signal.aborted) return;
    // 报告和引用使用同一份已保存快照。
    setCurrent(saved);setHits(saved.report_hits || []);setMessages(history.messages);
    window.dispatchEvent(new Event('opspilot:workspace-changed'));await refresh();
  }
  function fresh() {setLoadError('');setCurrent(null);setCaseTitle(''); setMessages([]); setHits([]); setEvents([]); setQuestion(''); setWorkflow('auto');setDraft('');setPhase('');setEvidencePanelOpen(false);setEvidenceFocus(null);setEvidenceKeys([]);liveWorkflow.current='auto';}
  async function ensureCase() {
    if (current) return current;
    const kind = workflow === 'history' ? 'history' : workflow === 'diagnose' || workflow === 'review' || /报错|异常|错误|超时|502|504|反复退出/.test(question) ? 'problem' : 'consult';
    const title = question.trim().slice(0, 70) || '网站维护';
    const created = await post<PersonalCase>('/api/support/cases', {title, site_id: siteId || null, kind, draft: !!onChat, symptom: kind === 'problem' ? question.trim().slice(0,2000) : ''});
    setCurrent(created); setCaseTitle(title); opened?.(created.id); await refresh(); return created;
  }
  function receiveRunEvent(event: AgentEvent, chosenWorkflow: Workflow) {
    if(event.run_id) run.current=event.run_id;
    setUpdated(Date.now());
    if(event.type==='existing_run' && ['running','cancel_requested'].includes(event.status || '')){
      setWaitingRun(true);setPhase('已连接到相同请求，正在等待结果');
    }
    if(event.type==='connected')setPhase('后台分析已连接');
    if(event.type==='reconnecting')setPhase('后台仍在分析，正在恢复连接');
    if(event.type==='resumed')setPhase(event.message || '已从保存的检查点继续');
    if(event.type==='phase'){liveWorkflow.current=String(event.workflow || chosenWorkflow);setPhase(event.message || '正在分析');}
    if(event.type==='tool_start')setPhase('正在检索相关资料');
    if(event.type==='tool')setPhase('已取得资料，正在组织回答');
    if(event.type==='answer_reset'){rawDraft.current='';setDraft('');}
    if(event.type==='token'&&liveWorkflow.current==='consult'){
      rawDraft.current+=event.delta || '';const summary=rawDraft.current.match(/"summary"\s*:\s*"((?:[^"\\]|\\.)*)/);
      if(summary)try{setDraft(JSON.parse('"'+summary[1]+'"'));}catch{/* 等待完整的转义字符。 */}
    }
    if(event.type==='cancelled'){setCancelling(false);setPhase(event.message || '已停止');}
    if(['phase','tool_start','tool','error','cancelled','existing_run','resumed','final'].includes(event.type)) setEvents(previous => [...previous, event]);
    if(event.type==='final'){setDraft('');setPhase(event.end_reason==='answered'?'本次分析已完成':'本次分析未完成，已有证据保留');setFailed(event.end_reason!=='answered');}
    if(event.type==='error'){setFailed(true);setPhase('分析未完成，已有证据保留');message.error(event.message || '分析未完成');}
  }
  async function send(text = question.trim(), chosenWorkflow = workflow, explicitCase?: PersonalCase) {
    if (!text || busy || cancelling || waitingRun || loadError) return;
    setBusy(true);setFailed(false);setLastQuestion(text);setStartedAt(Date.now());setPhase('正在开始本次分析');
    setDraft('');rawDraft.current='';liveWorkflow.current=chosenWorkflow;run.current='';setUpdated(Date.now());setEvents([]);
    const subscription = new AbortController();
    controller.current = subscription;
    try {
      const record = explicitCase || await ensureCase();
      if(subscription.signal.aborted) return;
      setQuestion('');
      const signature=JSON.stringify([record.id,text,chosenWorkflow,(record.evidence || []).map(e=>e.id)]);
      if(request.current.signature!==signature)request.current={id:newRequestId(),signature};
      setMessages(previous => [...previous, {role: 'user', content: text}]);
      await streamChat(record.id, text, event => receiveRunEvent(event, chosenWorkflow), subscription.signal, chosenWorkflow, request.current.id);
      await refreshResult(record.id, subscription.signal);
    } catch (error) {
      if(!subscription.signal.aborted){setFailed(true);setPhase('连接中断，重新打开问题可查看后台状态');message.error((error as Error).message);}
    } finally {if(controller.current===subscription){setBusy(false);setUpdated(Date.now());}}
  }
  function editSite(edit: boolean) {
    const site = edit ? sites.find(s => s.id === siteId) : undefined;
    setEditingSite(site?.id); setSiteOpen(true);
  }
  function openEvidence(kind?: 'initial' | 'check_result', step?: number) {
    const resolvedKind = kind ?? (current?.plan ? 'check_result' : 'initial');
    setEvidencePreview(null);
    setEvidenceKind(resolvedKind); setEvidenceText(''); setEvidenceLabel(resolvedKind === 'check_result' ? '复测结果' : '应用错误日志');
    setCollectedAt(''); setPreviewed(false); setEvidenceOpen(true);
    setEvidenceEndpoint(current?.problem?.endpoint || ''); setEvidenceStep(step);
  }
  async function preview() {
    setActionBusy(true);
    try {
      const value = await post<EvidencePreview>('/api/support/evidence/preview', {text: evidenceText, label: evidenceLabel, kind: evidenceKind,endpoint:evidenceEndpoint});
      setEvidenceText(value.text); setPreviewed(true);setEvidencePreview(value);
      message.info(`已遮蔽 ${value.replacements} 处常见敏感内容。`);
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  async function submitEvidence() {
    setActionBusy(true);
    try {
      const record = await ensureCase();
      const savedEv = await post<{replacements?: number; review_findings?: {count: number}[]}>(`/api/support/cases/${record.id}/evidence`, {
        text: evidenceText, label: evidenceLabel, kind: evidenceKind, collected_at: collectedAt,
        endpoint: evidenceEndpoint, step_index: evidenceStep ?? null, plan_revision: evidenceKind === 'check_result' ? record.plan?.revision : null,
      });
      const updatedCase = await api<PersonalCase>(`/api/support/cases/${record.id}`);
      setCurrent(updatedCase); setEvidenceOpen(false); setEvidenceText('');
      const nextWorkflow: Workflow = evidenceKind === 'check_result' ? 'review' : 'diagnose';
      const nextPrompt = question.trim() || (evidenceKind === 'check_result'
        ? '已提交检查后的新结果，请结合已确认方案核对原异常是否消失及仍缺少的信息。'
        : '请结合提交的日志和网站背景定位异常原因，并给出下一步检查方案。');
      setWorkflow(nextWorkflow);
      const masked = savedEv.replacements || 0;
      const ipCount = (savedEv.review_findings || []).reduce((n, f) => n + f.count, 0);
      message.success(`证据已保存${masked ? `（已遮蔽 ${masked} 处敏感信息）` : ''}${ipCount ? `，含 ${ipCount} 处 IP 请留意` : ''}，正在开始分析`);
      await refresh();
      setActionBusy(false);
      await send(nextPrompt, nextWorkflow, updatedCase);
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  async function confirmPlan() {
    if (!current) return; setActionBusy(true);
    try {
      const value = await post<{message: string}>(`/api/support/cases/${current.id}/confirm-plan`);
      setCurrent(await api<PersonalCase>(`/api/support/cases/${current.id}`)); message.info(value.message); await refresh();
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  async function saveCase() {
    if (!current) return; setActionBusy(true);
    try {
      const path = '/api/support/cases/' + current.id;
      if(recordAction==='resolve'){
        await post(path + '/status', {status:'user_resolved',note:solution,user_confirmed:confirmed});
        setCurrent(await api<PersonalCase>(path));
        message.success('已记录你确认的实际结局');
      }else{
        setCurrent(await post<PersonalCase>(path + '/save', {solution, user_confirmed: confirmed}));
        message.success('已保存，下次排查同一网站时可检索此案例。');
      }
      setRecordAction(null);await refresh();
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  async function trackIssue() {
    if (!current) return;
    setActionBusy(true);
    try {
      setCurrent(await post<PersonalCase>(`/api/support/cases/${current.id}/track`));
      message.success('已保存到问题中心');
      window.dispatchEvent(new Event('opspilot:workspace-changed'));
    } catch(error) {message.error((error as Error).message);}
    finally {setActionBusy(false);}
  }
  const report = current?.report, planConfirmed = !!current?.plan;
  const problemFlow = report && ['diagnose','review'].includes(report.workflow || 'diagnose');
  const guest=!!savedAuth()?.is_guest;
  const readOnly = guest || !!current?.archived || current?.status === 'user_resolved';
  const running = busy || waitingRun || cancelling;
  const lastAssistant = messages.map(m=>m.role).lastIndexOf('assistant');
  const renderMessages = () => messages.map((m,i)=>i===lastAssistant && report?.summary && m.content.trim().startsWith(report.summary.trim()) ? null : <article className={`support-message ${m.role}`} key={i}>{m.role!=='user'&&<span className="support-avatar" aria-label="OpsPilot">OP</span>}<div className="support-bubble"><div className="message-body">{m.content}</div></div></article>);
  const sampleChecks = current?.sample_checks || report?.program_checks;
  const verification = sampleChecks?.verification || report?.verification;
  const resultReady = verification?.verdict === 'sample_recovered';
  const latestEvidence = current?.evidence?.at(-1);
  const checkResultSaved = latestEvidence?.kind === 'check_result' && current?.status === 'evidence_submitted';
  const probeEvidenceSaved = latestEvidence?.source_kind === 'scheduled_http_probe' && current?.status === 'evidence_submitted';
  const needsPlanConfirmation = report?.workflow !== 'review' && !!report?.steps.length
    && current?.status !== 'needs_information' && (!planConfirmed || current?.plan?.revision !== current?.revision);
  const primaryAction = resultReady ? {label:'确认已解决', run:()=>{setSolution(current?.solution || '');setConfirmed(false);setRecordAction('resolve');}}
    : checkResultSaved ? {label:'核对新结果',run:()=>void send('请核对新样本，说明原问题是否仍出现及缺少的证据。','review')}
    : probeEvidenceSaved ? {label:'分析巡检异常',run:()=>void send('请分析这次定时 HTTP 巡检的异常证据，列出需要核实的原因。','diagnose')}
    : needsPlanConfirmation ? {label:'确认检查方案',run:()=>void confirmPlan()}
    : {label:'补充证据',run:()=>openEvidence()};
  const issueStatus = current?.status === 'sample_recovered' && !resultReady ? 'awaiting_results' : current?.status;
  useEffect(()=>{const node=conversation.current;if(node&&!issueDetail&&(node.scrollHeight-node.scrollTop-node.clientHeight<140||messages.length<=2))node.scrollTop=node.scrollHeight;},[messages,draft,phase,report,issueDetail]);
  const examples = ['不知道如何取日志？','网站返回 502，但我还没有日志，应该先收集什么信息？','日志出现 QueuePool 连接等待超时，这说明什么，下一步怎么检查？','Docker 里的 Python 应用反复退出，该怎么看日志？'];
  const history = <><div className="support-list-heading"><strong>{guest?'排查示例':'排查记录'}</strong>{!guest&&<Button size="small" disabled={running || actionBusy} onClick={()=>{fresh();setSiteId(undefined);opened?.('');}}>新排查</Button>}</div>
    <div className="support-case-list">{!cases.length&&<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无排查记录"/>}
      <ConversationList items={cases.map(c=>({id:c.id,title:c.title || '维护对话',detail:`${c.kind === 'problem' ? states[c.status] || c.status : c.kind === 'history' ? '历史查询' : '知识咨询'}${c.archived ? ' · 已归档' : ''}`}))}
        current={current?.id} busy={running || actionBusy} open={id=>{setHistoryOpen(false);void load(id);}}
        renamed={(id,title)=>{setCases(previous=>previous.map(item=>item.id===id?{...item,title}:item));if(current?.id===id)setCaseTitle(title);}}
        deleted={async ids=>{if(current && ids.includes(current.id)){fresh();setSiteId(undefined);opened?.('');}await refresh();}} />
    </div>
    {caseTotal>10&&<div className="support-list-footer"><Pagination simple current={casePage} total={caseTotal} pageSize={10} onChange={setCasePage}/></div>}</>;
  return <>
    <div className={issueDetail ? 'support-shell issue-detail' : 'support-shell'}>
      {!issueDetail&&<><aside className="support-conversations">{history}</aside>
        <Drawer className="assistant-history-drawer" placement="left" title="排查记录" size={300} open={historyOpen} onClose={()=>setHistoryOpen(false)}>{history}</Drawer></>}
      <main className="support-chat">
      <div className="support-chat-top"><Space className="support-heading-actions">
        {onChat&&<Button type="text" size="small" icon={<ArrowLeftOutlined aria-hidden="true"/>} onClick={onChat}>返回聊天</Button>}
        {!issueDetail&&<Button className="assistant-mobile-history" type="text" icon={<HistoryOutlined/>} aria-label="排查历史" onClick={()=>setHistoryOpen(true)}/>}
        <h3 title={caseTitle}>{loadingCase?'正在加载记录…':caseTitle||'网站排查'}</h3></Space>
        <div className="support-top-actions"><Select aria-label="选择网站" allowClear disabled={guest || !!current || running} placeholder="关联网站（选填）" value={siteId} onChange={setSiteId} options={sites.map(s=>({value:s.id,label:s.name}))}/><Button className="support-evidence-trigger" size="small" disabled={loadingCase} onClick={()=>{setEvidenceFocus(null);setEvidencePanelOpen(true);}}>证据与资料 · {(current?.evidence?.length||0)+hits.length}</Button></div></div>
      <div className="support-chat-scroll" ref={conversation} role="log" aria-label="维护对话" aria-live="polite">
        {loadingCase&&<Card className="section-bottom"><Skeleton active paragraph={{rows:5}}/></Card>}
        {issueDetail && current && <section className="issue-overview" aria-label="处理进度">
          <div className="issue-overview-main">
            <Space wrap size="small"><StatusTag status={issueStatus || 'investigating'}/>{current.archived && <Tag>已归档</Tag>}{current.profile?.name && <span>{current.profile.name}</span>}{current.problem?.endpoint && <code>{current.problem.endpoint}</code>}{current.evidence?.some(item=>item.source_kind==='scheduled_http_probe')&&<Tag>定时 HTTP 巡检</Tag>}</Space>
            <p>{(resultReady || report?.workflow==='review') && verification ? verification.reason : report?.summary || current.problem?.symptom || '补充日志后可以进行排查'}</p>
            {running && phase && <small role="status">{phase}</small>}
          </div>
          {!readOnly && <Button type="primary" disabled={running || actionBusy} onClick={primaryAction.run}>{primaryAction.label}</Button>}
        </section>}
        {issueDetail && current && !guest && <HandoffEditor key={current.id} record={current} changed={handoff=>setCurrent(previous=>previous?.id===current.id?{...previous,handoff}:previous)}/>}
        {loadError&&<Alert type="error" title="无法打开这条维护记录" description={loadError}/>}
        {!messages.length&&!report&&!busy&&<div className="support-greeting"><h3>网站遇到了什么问题？</h3><div className="support-suggestions">{examples.map(text=><Button key={text} size="small" disabled={running||readOnly||!!loadError} onClick={()=>setQuestion(text)}>{text}</Button>)}</div><CollectionGuide disabled={running||readOnly||!!loadError} label="证据收集向导" useResult={()=>openEvidence('initial')}/></div>}
        {messages.length>0 && (issueDetail
          ? <Collapse size="small" items={[{key:'conversation',label:'查看排障对话',children:renderMessages()}]} />
          : renderMessages())}
        {draft&&busy&&<div className="message-body support-draft">{draft}</div>}
        <div className="support-report-flow">
      {report && !problemFlow && <section className="support-answer" aria-label={workflowNames[report.workflow || 'consult']}><div className="message-body">{report.summary}</div></section>}
      {!!current?.related_cases?.length && <Card title="查到的历史处理记录">{current.related_cases.map(c=><Card size="small" className="section-bottom" key={c.session_id}><Space wrap><StatusTag status={c.outcome}/>{c.archived && <Tag>已归档</Tag>}</Space><p>{c.solution}</p><Button onClick={()=>load(c.session_id)}>查看这次问题的证据与处理过程</Button></Card>)}</Card>}
      {report && problemFlow && <>
        {current && <ResultCard record={current} hits={hits} showSummary={!issueDetail} disabled={running || actionBusy || readOnly} confirmPlan={confirmPlan} onSource={onSource} onEvidence={(id,start,end)=>{setEvidenceFocus({id,start,end});setEvidenceKeys([id]);setEvidencePanelOpen(true);}}/>}
        {current?.plan && <PlanChecklist record={current} disabled={running || actionBusy || readOnly} changed={setCurrent} evidence={index => openEvidence('check_result',index)} />}
        <Card title="处理结果核对">
          {verification && <Alert className="section-gap" type={resultReady ? 'success' : 'info'} title={verdicts[verification.verdict]} description={verification.reason} />}
          <SampleResults value={sampleChecks} />
          <Space wrap>{!issueDetail && !guest && (current?.draft?<Button disabled={running||actionBusy} onClick={()=>void trackIssue()}>保存到问题中心</Button>
              :<Button onClick={()=>onIssues(current?.id)}>在问题中心打开</Button>)}
          <Button disabled={running || actionBusy || readOnly} onClick={() => {setSolution(current?.solution || '');setConfirmed(false);setRecordAction('archive');}}>归档当前记录</Button></Space>
        </Card>
      </>}
      {!report && messages.length > 0 && !busy && <Alert type="info" title="未获得有效的新报告，可补充准确报错后重试；已有记录不会自动表示问题已解决。" />}

          {(phase||current)&&<ProcessFold phase={phase} failed={failed} events={events} startedAt={startedAt} updatedAt={updated} cancelling={cancelling} action={!running&&lastQuestion?<Button type="text" size="small" disabled={readOnly||!!loadError||actionBusy} onClick={()=>{request.current={id:'',signature:''};setQuestion(lastQuestion);}}>{failed?'重试本次问题':'重新分析此问题'}</Button>:undefined}>
            {current&&<MaintenanceTimeline caseId={current.id} revision={current.updated_at || current.revision} severity={current.severity} labels={current.labels}/>}
          </ProcessFold>}
        </div>
      </div>
      <div className="support-composer">
        <div className="support-composer-box"><Input.TextArea aria-label="网站排障问题" disabled={readOnly||!!loadError} placeholder="描述异常现象，或点「补充证据」提交日志自动脱敏后分析" value={question} maxLength={6000} autoSize={{minRows:2,maxRows:5}} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.ctrlKey&&e.key==='Enter')void send();}}/>
          <div className="support-composer-tools"><div className="support-input-actions"><Button size="small" disabled={running||actionBusy||(!guest&&readOnly)||!!loadError} onClick={()=>guest?message.info('登录个人账号后可上传日志并排查'):openEvidence()}>补充证据</Button></div>
            {running&&!loadingCase?<Button danger disabled={!run.current||cancelling} onClick={stop}>停止生成</Button>:<Button type="primary" disabled={!question.trim()||actionBusy||readOnly||!!loadError||loadingCase} onClick={()=>void send()}>发送</Button>}
          </div>
        </div>
      </div>
    </main></div>
    <Drawer title="本次证据与资料" width={430} open={evidencePanelOpen} onClose={()=>setEvidencePanelOpen(false)} afterOpenChange={open=>{
      const line=evidenceLine.current, pre=line?.parentElement;
      if(open && line && pre){pre.scrollTop=line.offsetTop-24;line.focus({preventScroll:true});}
    }}><div className="page-stack">
      <div className="support-site-tools"><Space wrap><Button size="small" disabled={guest||!!current||running} onClick={()=>editSite(false)}>添加网站</Button><Button size="small" disabled={guest||!siteId||!!current||running} onClick={()=>editSite(true)}>修改背景</Button><Button size="small" type="link" onClick={onKnowledge}>维护手册</Button></Space>{current?.profile?.name&&<p className="muted section-gap"><strong>{current.profile.name}</strong> · {[current.profile.framework,current.profile.deployment,current.profile.database,current.profile.versions].filter(Boolean).join(' · ')||'技术环境尚未填写'}</p>}</div>
      <Card size="small" title={`本次证据 · ${current?.evidence?.length || 0}`}>{!current?.evidence?.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="可以先咨询；排查具体故障时再提交日志" />}
        <Collapse activeKey={evidenceKeys} onChange={keys=>setEvidenceKeys(Array.isArray(keys)?keys:[keys])} items={(current?.evidence || []).map(e => ({key:e.id,label:<>{e.label}<small> · {e.kind === 'check_result' ? '操作后结果' : '初始/补充证据'}</small></>,children:<>
          <small>{e.id} · {e.collected_at || '未填写采集时间'}</small>{e.summary&&<EvidenceSummary value={e.summary} onLines={lines=>{if(lines.length)setEvidenceFocus({id:e.id,start:Math.min(...lines),end:Math.max(...lines),lines});}}/>}<pre className="evidence-lines" aria-label={`${e.label}原文`}>{e.text.split('\n').map((line,index)=>{
            const highlighted=evidenceFocus?.id===e.id && (evidenceFocus.lines?evidenceFocus.lines.includes(index+1):index+1>=evidenceFocus.start && index+1<=evidenceFocus.end);
            const first=highlighted && index+1===evidenceFocus?.start;
            return <span key={index} className={`evidence-line${highlighted?' highlighted':''}`} ref={first?evidenceLine:undefined} tabIndex={first?-1:undefined}>{`L${index+1}: ${line || ' '}`}</span>;
          })}</pre>
        </>}))}/>
      </Card>
      <Card size="small" title={<>资料依据 · {hits.length}</>} extra={<Button type="text" size="small" aria-label="资料依据说明" onClick={()=>message.info('手册是参考依据，不是本次故障的实时证据。')}>说明</Button>}>{!hits.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="检索后在这里查看出处"/>}{hits.map(h => <button key={h.chunk_id} className="evidence-card" onClick={() => onSource(h)}><strong>[{h.citation_id}] {h.doc_title}</strong><Tag>{report?.references.includes(h.citation_id || 0)?'本次回答引用':'检索候选'}</Tag><small>{h.source_kind === 'official' ? `官方资料整理 · ${h.checked_at}` : `网站手册 · ${h.version ? `v${h.version}` : '正文'}`}</small><span>查看适用版本与原文 →</span></button>)}</Card>
      {!!current?.reports?.length && <Card size="small" title="历次判断与资料版本"><Collapse items={current.reports.map(r => ({key: String(r.revision), label: `第 ${r.revision} 次分析`, children: <><p>{r.report.summary}</p><small>{verdicts[r.report.verification.verdict]}</small>{r.hits.map(h => <Button type="link" style={{whiteSpace: 'normal', height: 'auto'}} key={h.chunk_id} onClick={() => onSource(h)}>[{h.citation_id}] {h.doc_title}{h.version ? ` v${h.version}` : ''}</Button>)}</>}))} /></Card>}
    </div></Drawer>
    <SiteEditor open={siteOpen} site={sites.find(s => s.id === editingSite)} close={() => setSiteOpen(false)} saved={s => {setSiteId(s.id); setSiteOpen(false); refresh().catch(e => message.error(e.message));}} />
    <Modal open={evidenceOpen} title={evidenceKind === 'check_result' ? '补充复测结果' : '补充日志或检查证据'} onCancel={() => setEvidenceOpen(false)} footer={<Space><Button onClick={() => setEvidenceOpen(false)}>取消</Button><Button loading={actionBusy} disabled={!evidenceText.trim()} onClick={preview}>脱敏预览</Button><Button type="primary" disabled={!evidenceText.trim() || !evidenceLabel.trim()} loading={actionBusy} onClick={submitEvidence}>提交并分析</Button></Space>}><label className="form-field">上传日志片段（TXT / LOG / JSON，UTF-8，最多64KB / 12000字符）<input aria-label="上传日志片段" type="file" accept=".txt,.log,.json" onChange={async e=>{const f=e.target.files?.[0];if(!f)return;setActionBusy(true);try{const body=new FormData();body.append('file',f);const r=await api<EvidencePreview>('/api/maintenance/evidence/file-preview',{method:'POST',body});setEvidenceText(r.text);setEvidenceLabel((r.filename||f.name).slice(0,100));setPreviewed(true);setEvidencePreview(r);}catch(error){message.error((error as Error).message);}finally{setActionBusy(false);}}}/></label>
      <Alert type="info" title="提交时自动遮蔽常见密码、令牌和邮箱；请留意日志中的公网 IP 等信息。" />
      <div className="form-field"><label htmlFor="evidence-label">这是什么证据</label><Input id="evidence-label" value={evidenceLabel} maxLength={100} onChange={e => setEvidenceLabel(e.target.value)} /></div>
      <div className="form-field"><label htmlFor="evidence-time">采集时间 / 时区 / 请求范围（可选）</label><Input id="evidence-time" value={collectedAt} maxLength={80} onChange={e => setCollectedAt(e.target.value)} placeholder="例如 2026-11-11 21:00 北京时间，重试了3次下单请求" /></div>
      <div className="form-field"><label htmlFor="evidence-endpoint">对应接口（只有HTTP状态行时请填写）</label><Input id="evidence-endpoint" value={evidenceEndpoint} maxLength={200} onChange={e => setEvidenceEndpoint(e.target.value)} placeholder="/orders；日志中保留带时区时间戳，如2026-11-12T10:00:00+08:00" /></div>
      {evidenceStep !== undefined && <Tag>关联已确认方案的检查 {evidenceStep + 1}</Tag>}
      <Input.TextArea className="section-gap" aria-label="证据文本" value={evidenceText} maxLength={12000} rows={10} onChange={e => {setEvidenceText(e.target.value); setPreviewed(false);setEvidencePreview(null);}} placeholder="粘贴相关日志、报错、脱敏配置片段，或操作后重新请求的结果…" />
      {previewed&&evidencePreview&&<><RedactionNotice value={evidencePreview}/>{evidencePreview.summary&&<EvidenceSummary value={evidencePreview.summary}/>}</>}
    </Modal>
    <Modal open={!!recordAction} title={recordAction==='resolve'?'由你确认问题已解决':'归档处理记录'} onCancel={() => setRecordAction(null)} onOk={saveCase} confirmLoading={actionBusy} okText={recordAction==='resolve'?'确认解决':'归档记录'} okButtonProps={{disabled: !confirmed || !solution.trim()}}>
      <p>{recordAction==='resolve'?'填写你实际复测的操作和结果。':'记录实际做过的操作、结果或暂停原因。归档保留当前问题状态。'}</p>
      <Input.TextArea aria-label="处理记录" value={solution} maxLength={2000} rows={5} onChange={e => setSolution(e.target.value)} placeholder="我检查了什么、结果如何、哪些问题仍未确认…" />
      <Checkbox className="section-gap" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>{recordAction==='resolve'?'我已复测并确认实际问题已解决':'我已核对这份记录，同意保留为本网站的处理参考'}</Checkbox>
    </Modal>
  </>;
}
