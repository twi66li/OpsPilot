import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Collapse, Drawer, Empty, Input, Modal, Pagination, Select, Space, Steps, Tag } from 'antd';
import { api, newRequestId, post, streamChat } from './api';
import type { AgentEvent, ChatMessage, Hit, PageData, PersonalCase, Site, SupportEntry, Workflow } from './types';
import { caseStates as states, workflowNames, workflows } from './supportTypes';
import SiteEditor from './SiteEditor';
import CollectionGuide from './CollectionGuide';
import MaintenanceTimeline from './MaintenanceTimeline';
import SampleResults from './SampleResults';
import PlanChecklist from './PlanChecklist';
import ConversationList from './ConversationList';

const verdicts: Record<string, string> = {inconclusive: '证据仍不足', still_failing: '样本中仍有异常', improved: '样本显示改善', sample_recovered: '提交样本支持原症状不再复现'};

export default function PersonalSupport({entry, onSource, onKnowledge, onIssues, opened}: {opened?:(id:string)=>void; entry?: SupportEntry; onSource: (hit: Hit) => void; onKnowledge: () => void; onIssues: () => void}) {
  const {message} = App.useApp();
  const [cases, setCases] = useState<PersonalCase[]>([]), [sites, setSites] = useState<Site[]>([]);
  const [current, setCurrent] = useState<PersonalCase | null>(null), [siteId, setSiteId] = useState<string | undefined>();
  const [caseTitle, setCaseTitle] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]), [question, setQuestion] = useState(''), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]), [hits, setHits] = useState<Hit[]>([]);
  const [siteOpen, setSiteOpen] = useState(false), [editingSite, setEditingSite] = useState<string | undefined>();
  const [workflow, setWorkflow] = useState<Workflow>('auto'), [evidenceEndpoint, setEvidenceEndpoint] = useState(''), [evidenceStep, setEvidenceStep] = useState<number | undefined>();
  const [evidenceOpen, setEvidenceOpen] = useState(false), [evidencePanelOpen, setEvidencePanelOpen] = useState(false), [evidenceText, setEvidenceText] = useState(''), [evidenceLabel, setEvidenceLabel] = useState('应用错误日志');
  const [evidenceKind, setEvidenceKind] = useState<'initial' | 'check_result'>('initial'), [collectedAt, setCollectedAt] = useState(''), [previewed, setPreviewed] = useState(false);
  const [actionBusy, setActionBusy] = useState(false), [saveOpen, setSaveOpen] = useState(false), [solution, setSolution] = useState(''), [confirmed, setConfirmed] = useState(false);
  const controller = useRef<AbortController | null>(null), conversation = useRef<HTMLDivElement>(null);
  const request = useRef({id:'',signature:''});
  const [waitingRun,setWaitingRun]=useState(false),[casePage,setCasePage]=useState(1),[caseTotal,setCaseTotal]=useState(0);
  const run = useRef(''), liveWorkflow = useRef('auto'), rawDraft = useRef('');
  const [phase,setPhase] = useState(''), [draft,setDraft] = useState(''), [updated,setUpdated] = useState(''), [cancelling,setCancelling] = useState(false);
  const [startedAt,setStartedAt]=useState(''),[loadError,setLoadError]=useState('');
  const [lastQuestion,setLastQuestion] = useState(''), [failed,setFailed] = useState(false);
  const refresh = useCallback(async () => {
    const [records, profiles] = await Promise.all([api<PageData<PersonalCase>>(`/api/workspace/cases?page=${casePage}&page_size=10`), api<Site[]>('/api/support/sites')]);
    setCases(records.items);setCaseTotal(records.total); setSites(profiles);
  }, [casePage]);
  useEffect(() => {refresh().catch(e => message.error(e.message)); return () => controller.current?.abort();}, [refresh, message]);
  useEffect(() => {if (entry?.caseId) {if(entry.caseId!==current?.id)void load(entry.caseId);} else {fresh(); setSiteId(entry?.siteId); setWorkflow(entry?.workflow || 'auto'); setQuestion(entry?.question || '');}}, [entry]);
  useEffect(()=>{if(!cancelling&&!waitingRun)return;const timer=setInterval(()=>{if(run.current)api<{status:string}>(`/api/agent/runs/${run.current}`).then(r=>{if(!['running','cancel_requested'].includes(r.status)){setCancelling(false);setWaitingRun(false);setPhase(['completed','answered'].includes(r.status)?'分析已完成':'运行已结束，已有证据保留');if(current)void load(current.id);}}).catch(()=>{});},1500);return()=>clearInterval(timer);},[cancelling,waitingRun,current?.id]);
  async function stop() {if(!run.current)return;setCancelling(true);setPhase('正在停止，请等待当前请求收尾');try{await post(`/api/agent/runs/${run.current}/cancel`);controller.current?.abort();}catch(e){setCancelling(false);message.error((e as Error).message);}}
  async function load(id: string) {
    setBusy(true);setCurrent(null);setCaseTitle('');setMessages([]);setHits([]);setLoadError(''); setEvents([]); setWorkflow('auto'); setQuestion('');setDraft('');setPhase('');liveWorkflow.current='auto';
    try {
      const [record, history] = await Promise.all([api<PersonalCase>(`/api/support/cases/${id}`), api<{title: string; messages: ChatMessage[]}>(`/api/sessions/${id}`)]);
      setCurrent(record); setCaseTitle(history.title); opened?.(record.id); setSiteId(record.site_id); setMessages(history.messages); setHits(record.report_hits || []);
    } catch (error) {setLoadError((error as Error).message);message.error((error as Error).message);} finally {setBusy(false);}
  }
  function fresh() {setLoadError('');setCurrent(null);setCaseTitle(''); setMessages([]); setHits([]); setEvents([]); setQuestion(''); setWorkflow('auto');setDraft('');setPhase('');liveWorkflow.current='auto';}
  async function ensureCase() {
    if (current) return current;
    const kind = workflow === 'history' ? 'history' : workflow === 'diagnose' || workflow === 'review' || /报错|异常|错误|超时|502|504|反复退出/.test(question) ? 'problem' : 'consult';
    const title = question.trim().slice(0, 70) || '网站维护';
    const created = await post<PersonalCase>('/api/support/cases', {title, site_id: siteId || null, kind, symptom: kind === 'problem' ? question.trim().slice(0,2000) : ''});
    setCurrent(created); setCaseTitle(title); opened?.(created.id); await refresh(); return created;
  }
  async function send() {
    const text = question.trim(); if (!text || busy || cancelling || waitingRun || loadError) return;
    setBusy(true); setFailed(false); setLastQuestion(text); setStartedAt(new Date().toLocaleTimeString());setPhase('正在开始本次分析');setDraft('');rawDraft.current='';liveWorkflow.current=workflow;run.current='';setUpdated(new Date().toLocaleTimeString());setEvents([]); controller.current = new AbortController();
    try {
      const record = await ensureCase(); setQuestion('');
      const signature=JSON.stringify([record.id,text,workflow,(record.evidence || []).map(e=>e.id)]);
      if(request.current.signature!==signature)request.current={id:newRequestId(),signature};
      let answer = '', runId = '';
      setMessages(previous => [...previous, {role: 'user', content: text}]);
      await streamChat(record.id, text, event => {
        if(event.run_id)run.current=event.run_id;
        setUpdated(new Date().toLocaleTimeString());
        if(event.type==='existing_run'){if(['running','cancel_requested'].includes(event.status || '')){setWaitingRun(true);setPhase('已连接到相同请求，正在等待结果');}else if(event.answer){answer=event.answer;setPhase('已读取同一次请求的结果');}}
        if(event.type==='phase'){liveWorkflow.current=String(event.workflow || workflow);setPhase(event.message || '正在分析');}
        if(event.type==='tool_start')setPhase('正在检索相关资料');
        if(event.type==='tool')setPhase('已取得资料，正在组织回答');
        if(event.type==='answer_reset'){rawDraft.current='';setDraft('');}
        if(event.type==='token'&&liveWorkflow.current==='consult'){
          rawDraft.current+=event.delta || '';const summary=rawDraft.current.match(/"summary"\s*:\s*"((?:[^"\\]|\\.)*)/);
          if(summary)try{setDraft(JSON.parse('"'+summary[1]+'"'));}catch{/* 等待完整的转义字符。 */}
        }
        if(event.type==='cancelled'){setCancelling(false);setPhase(event.message || '已停止');}
        if (event.run_id) runId = event.run_id;
        if (['tool_start', 'tool', 'error'].includes(event.type)) setEvents(previous => [...previous, event]);
        if (event.type === 'final') {answer = event.answer || '';setDraft('');setPhase('本次分析已完成');setFailed(event.end_reason!=='answered'); setHits(event.hits || []);}
        if (event.type === 'error') {setFailed(true);setPhase('分析未完成，已有证据保留');message.error(event.message || '分析未完成');}
      }, controller.current.signal, workflow, request.current.id);
      if (answer) setMessages(previous => [...previous, {role: 'assistant', content: answer, meta: {run_id: runId}}]);
      const [updated, history] = await Promise.all([api<PersonalCase>(`/api/support/cases/${record.id}`), api<{messages: ChatMessage[]}>(`/api/sessions/${record.id}`)]);
      setCurrent(updated); setMessages(history.messages); await refresh();
    } catch (error) {if ((error as Error).name !== 'AbortError'){setFailed(true);setPhase('连接中断，已有记录保留');message.error((error as Error).message);}}
    finally {setBusy(false);}
  }
  function editSite(edit: boolean) {
    const site = edit ? sites.find(s => s.id === siteId) : undefined;
    setEditingSite(site?.id); setSiteOpen(true);
  }
  function openEvidence(kind: 'initial' | 'check_result', step?: number) {
    setEvidenceKind(kind); setEvidenceText(''); setEvidenceLabel(kind === 'check_result' ? '操作后的检查结果' : '应用错误日志');
    setCollectedAt(''); setPreviewed(false); setEvidenceOpen(true);
    setEvidenceEndpoint(current?.problem?.endpoint || ''); setEvidenceStep(step);
  }
  async function preview() {
    setActionBusy(true);
    try {
      const value = await post<{text: string; replacements: number}>('/api/support/evidence/preview', {text: evidenceText, label: evidenceLabel, kind: evidenceKind});
      setEvidenceText(value.text); setPreviewed(true);
      message.info(`已替换 ${value.replacements} 处常见敏感内容，请继续检查后确认提交。`);
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  async function submitEvidence() {
    setActionBusy(true);
    try {
      const record = await ensureCase();
      await post(`/api/support/cases/${record.id}/evidence`, {text: evidenceText, label: evidenceLabel, kind: evidenceKind, collected_at: collectedAt,
        endpoint:evidenceEndpoint, step_index:evidenceStep ?? null, plan_revision:evidenceKind === 'check_result' ? record.plan?.revision : null});
      setCurrent(await api<PersonalCase>(`/api/support/cases/${record.id}`)); setEvidenceOpen(false); setEvidenceText('');
      if (!question.trim()) setQuestion(evidenceKind === 'check_result' ? '我已提交新的检查结果，请结合之前确认的方案核对原问题是否仍存在，还有什么证据不足？' : '请结合我提交的日志和网站背景，解释异常，并给出下一步最需要做的检查。');
      setWorkflow(evidenceKind === 'check_result' ? 'review' : 'diagnose');
      message.success('证据已保存，点击「发送」开始分析。'); await refresh();
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
      const value = await post<PersonalCase>(`/api/support/cases/${current.id}/save`, {solution, user_confirmed: confirmed});
      setCurrent(value); setSaveOpen(false); await refresh(); message.success('已保存，下次排查同一网站时可检索此案例。');
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  const report = current?.report, planConfirmed = !!current?.plan;
  const problemFlow = report && ['diagnose','review'].includes(report.workflow || 'diagnose');
  const readOnly = !!current?.archived || current?.status === 'user_resolved';
  const running = busy || waitingRun || cancelling;
  useEffect(()=>{const node=conversation.current;if(node)node.scrollTop=node.scrollHeight;},[messages,draft,phase,report]);
  const examples = ['不知道如何取日志？','网站返回 502，但我还没有日志，应该先收集什么信息？','日志出现 QueuePool 连接等待超时，这说明什么，下一步怎么检查？','Docker 里的 Python 应用反复退出，该怎么看日志？'];
  return <>
    <div className="support-shell"><aside className="support-conversations">
      <div className="support-list-heading"><strong>对话</strong><Button size="small" disabled={running || actionBusy} onClick={()=>{fresh();opened?.('');}}>新对话</Button></div>
      <div className="support-case-list">{!cases.length&&<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话"/>}<ConversationList items={cases.map(c=>({id:c.id,title:c.title || '维护对话',detail:`${c.kind === 'problem' ? states[c.status] || c.status : c.kind === 'history' ? '历史查询' : '知识咨询'}${c.archived ? ' · 已归档' : ''}`}))} current={current?.id} busy={running || actionBusy} open={id=>void load(id)} deleted={async ids=>{if(current && ids.includes(current.id)){fresh();setSiteId(undefined);}await refresh();}} /></div>
      <div className="support-list-footer">{caseTotal>10&&<Pagination simple current={casePage} total={caseTotal} pageSize={10} onChange={setCasePage}/>}<Button type="link" onClick={onIssues}>全部问题 →</Button></div>
    </aside><main className="support-chat">
      <div className="support-chat-top"><h3 title={caseTitle||'新对话'}>{caseTitle||'新对话'}</h3><div className="support-top-actions"><Button className="support-mobile-issues" size="small" onClick={onIssues}>问题</Button><Select aria-label="选择网站" allowClear disabled={!!current || running} placeholder="关联网站（选填）" value={siteId} onChange={setSiteId} options={sites.map(s=>({value:s.id,label:s.name}))}/><Button size="small" onClick={()=>setEvidencePanelOpen(true)}>证据与资料 · {(current?.evidence?.length||0)+hits.length}</Button></div></div>
      <div className="support-chat-scroll" ref={conversation} role="log" aria-label="维护对话" aria-live="polite">
        {loadError&&<Alert type="error" title="无法打开这条维护记录" description={loadError}/>}
        {!messages.length&&!report&&!busy&&<div className="support-greeting"><h3>网站遇到了什么问题？</h3><div className="support-suggestions">{examples.map(text=><Button key={text} size="small" disabled={running||readOnly||!!loadError} onClick={()=>setQuestion(text)}>{text}</Button>)}</div><CollectionGuide disabled={running||readOnly||!!loadError} label="证据收集向导" useResult={()=>openEvidence('initial')}/></div>}
        {messages.map((m,i)=><article className={`support-message ${m.role}`} key={i}>{m.role!=='user'&&<span className="support-avatar" aria-label="OpsPilot">OP</span>}<div className="support-bubble"><div className="message-body">{m.content}</div></div></article>)}
        {phase&&<article className="support-message assistant"><span className="support-avatar" aria-hidden="true">OP</span><div className="support-bubble"><div className={failed?'support-phase failed':'support-phase'} role="status">{phase}</div>{startedAt&&<small>开始于 {startedAt} · 最后更新 {updated}{cancelling?'；当前请求可能仍产生模型用量':''}</small>}{draft&&busy&&<div className="message-body support-draft">{draft}</div>}{!running&&lastQuestion&&<Button size="small" onClick={()=>{request.current={id:'',signature:''};setQuestion(lastQuestion);}}>{failed?'重试本次问题':'重新分析此问题'}</Button>}{!!events.length&&<Collapse ghost size="small" items={[{key:'progress',label:'查看检索过程',children:events.map((e,i)=><p className="muted" key={i}>{e.type==='error'?e.message:e.type==='tool_start'?'正在检索…':e.summary||'检索结束'}</p>)}]}/>}</div></article>}
        <div className="support-report-flow">
      {report && !problemFlow && <Card title={workflowNames[report.workflow || 'consult']}><div className="message-body">{report.summary}</div></Card>}
      {!!current?.related_cases?.length && <Card title="查到的历史处理记录">{current.related_cases.map(c=><Card size="small" className="section-bottom" key={c.session_id}><Space wrap><Tag>{states[c.outcome] || c.outcome}</Tag>{c.archived && <Tag>已归档</Tag>}</Space><p>{c.solution}</p><Button onClick={()=>load(c.session_id)}>查看这次问题的证据与处理过程</Button></Card>)}</Card>}
      {report && problemFlow && <>
        <Card title="当前判断"><p className="support-summary">{report.summary}</p><h4>日志直接说明了什么</h4>
          {!report.observations.length && <p className="muted">尚未提供可核对的日志行，当前只有基础指导。</p>}
          {report.observations.map((o, i) => <div key={i}><Tag>{o.evidence_id} L{o.line_start}–L{o.line_end}</Tag><p>{o.meaning}</p><pre className="source-text">{o.quote}</pre></div>)}
          {!!report.hypotheses.length && <><h4>待验证的可能原因</h4><ul>{report.hypotheses.map((h, i) => <li key={i}>{h}</li>)}</ul></>}
          {!!report.missing_info.length && <Alert type="info" title="下一步最需要补充" description={<ul>{report.missing_info.map((g, i) => <li key={i}>{g}</li>)}</ul>} />}
        </Card>
        {!!report.steps.length && <Card title="建议检查步骤（由你在自己的环境操作）">{report.steps.map((s, i) => <div className="check-step" key={i}><h4><Tag color={s.risk === '只读' ? 'green' : 'orange'}>{s.risk}</Tag>第 {i + 1} 步 · {s.location}</h4><pre className="source-text">{s.action}</pre><p><strong>正常时：</strong>{s.normal}<br /><strong>异常时：</strong>{s.abnormal}</p>{s.rollback && <Alert type="warning" title="影响与回退" description={s.rollback} />}</div>)}
          <Button disabled={busy || actionBusy || readOnly || current?.plan?.revision === current?.revision} onClick={confirmPlan}>我确认检查方案，准备自行操作</Button><p className="muted">此按钮只记录你的确认，不会执行命令、修改配置或标记恢复。</p>
        </Card>}
        {current?.plan && <PlanChecklist record={current} disabled={busy || actionBusy || readOnly} changed={setCurrent} evidence={index => openEvidence('check_result',index)} />}
        <Card title="处理结果核对"><Steps size="small" current={current?.saved ? 3 : current?.status === 'sample_recovered' ? 2 : planConfirmed ? 1 : 0} items={[{title: '确认方案'}, {title: '提交新结果'}, {title: '核对证据'}, {title: '保存案例'}]} />
          <Alert className="section-gap" type={report.verification.verdict === 'sample_recovered' ? 'success' : 'info'} title={verdicts[report.verification.verdict]} description={report.verification.reason} />
          <SampleResults value={current?.sample_checks || report.program_checks} />
          <p className="muted">判断仅针对提交样本。实际结局由你在问题中心确认；归档不会自动标记解决。</p><Space wrap><Button disabled={busy || actionBusy || readOnly || !planConfirmed} onClick={() => openEvidence('check_result')}>添加新的检查结果</Button><Button onClick={onIssues}>确认问题结局 / 管理记录</Button><Button disabled={busy || actionBusy || readOnly} onClick={() => {setSolution(current?.solution || ''); setConfirmed(false); setSaveOpen(true);}}>归档当前记录</Button></Space>
        </Card>
      </>}
      {!report && messages.length > 0 && !busy && <Alert type="info" title="未获得有效的新报告，可补充准确报错后重试；已有记录不会自动表示问题已解决。" />}

          {current&&<MaintenanceTimeline caseId={current.id} revision={current.updated_at || current.revision} severity={current.severity} labels={current.labels}/>}
        </div>
      </div>
      <div className="support-composer">
        {readOnly&&<Alert className="section-bottom" type="info" title={current?.archived?'当前记录已归档，原问题状态保留。':'这个问题已由你确认解决。'} description="可以查看历史和证据；需要继续排查时，请在问题中心重新打开。" action={<Button onClick={onIssues}>前往问题中心</Button>}/>}
        <div className="support-composer-box"><Input.TextArea aria-label="网站排障问题" disabled={readOnly||!!loadError} placeholder="描述异常、发生时间和影响。日志请通过「添加日志」脱敏后提交" value={question} maxLength={6000} autoSize={{minRows:2,maxRows:5}} onChange={e=>setQuestion(e.target.value)} onKeyDown={e=>{if(e.ctrlKey&&e.key==='Enter')void send();}}/>
          <div className="support-composer-tools"><div className="support-input-actions"><Button size="small" disabled={running||actionBusy||readOnly||!!loadError} onClick={()=>openEvidence('initial')}>添加日志</Button>{planConfirmed&&<Button size="small" disabled={running||actionBusy||readOnly} onClick={()=>openEvidence('check_result')}>提交操作后结果</Button>}<Select aria-label="维护任务类型" disabled={running||readOnly} value={workflow} onChange={setWorkflow} options={workflows.map(w=>({...w,label:({auto:'自动判断任务',consult:'知识咨询',diagnose:'异常排查',review:'结果核对',history:'历史查询'}[w.value])}))}/></div>
            {running?<Button danger disabled={!run.current||cancelling} onClick={stop}>停止生成</Button>:<Button type="primary" disabled={!question.trim()||actionBusy||readOnly||!!loadError} onClick={send}>发送</Button>}
          </div>
        </div>
      </div>
    </main></div>
    <Drawer title="本次证据与资料" width={430} open={evidencePanelOpen} onClose={()=>setEvidencePanelOpen(false)}><div className="page-stack">
      <div className="support-site-tools"><Space wrap><Button size="small" disabled={!!current||running} onClick={()=>editSite(false)}>添加网站</Button><Button size="small" disabled={!siteId||!!current||running} onClick={()=>editSite(true)}>修改背景</Button><Button size="small" type="link" onClick={onKnowledge}>维护手册</Button></Space>{current?.profile?.name&&<p className="muted section-gap"><strong>{current.profile.name}</strong> · {[current.profile.framework,current.profile.deployment,current.profile.database,current.profile.versions].filter(Boolean).join(' · ')||'技术环境尚未填写'}</p>}</div>
      <Card size="small" title={`本次证据 · ${current?.evidence?.length || 0}`}>{!current?.evidence?.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="可以先咨询；排查具体故障时再提交日志" />}
        <Collapse items={(current?.evidence || []).map(e => ({key: e.id, label: <>{e.label}<small> · {e.kind === 'check_result' ? '操作后结果' : '初始/补充证据'}</small></>, children: <><small>{e.id} · {e.collected_at || '未填写采集时间'}</small><pre>{e.text.split('\n').map((line, i) => `L${i + 1}: ${line}`).join('\n')}</pre></>}))} />
      </Card>
      <Card size="small" title={`资料依据 · ${hits.length}`}><p className="muted">手册是参考依据，不是本次故障的实时证据。</p>{!hits.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="检索后在这里查看出处"/>}{hits.map(h => <button key={h.chunk_id} className="evidence-card" onClick={() => onSource(h)}><strong>[{h.citation_id}] {h.doc_title}</strong><Tag>{report?.references.includes(h.citation_id || 0)?'本次回答引用':'检索候选'}</Tag><small>{h.source_kind === 'official' ? `官方资料整理 · ${h.checked_at}` : `网站手册 · ${h.version ? `v${h.version}` : '正文'}`}</small><span>查看适用版本与原文 →</span></button>)}</Card>
      {!!current?.reports?.length && <Card size="small" title="历次判断与资料版本"><Collapse items={current.reports.map(r => ({key: String(r.revision), label: `第 ${r.revision} 次分析`, children: <><p>{r.report.summary}</p><small>{verdicts[r.report.verification.verdict]}</small>{r.hits.map(h => <Button type="link" style={{whiteSpace: 'normal', height: 'auto'}} key={h.chunk_id} onClick={() => onSource(h)}>[{h.citation_id}] {h.doc_title}{h.version ? ` v${h.version}` : ''}</Button>)}</>}))} /></Card>}
    </div></Drawer>
    <SiteEditor open={siteOpen} site={sites.find(s => s.id === editingSite)} close={() => setSiteOpen(false)} saved={s => {setSiteId(s.id); setSiteOpen(false); refresh().catch(e => message.error(e.message));}} />
    <Modal open={evidenceOpen} title={evidenceKind === 'check_result' ? '提交操作后的新结果' : '添加日志或检查证据'} onCancel={() => setEvidenceOpen(false)} footer={<Space><Button onClick={() => setEvidenceOpen(false)}>取消</Button><Button loading={actionBusy} disabled={!evidenceText.trim()} onClick={preview}>脱敏预览</Button><Button type="primary" disabled={!previewed || !evidenceText.trim() || !evidenceLabel.trim()} loading={actionBusy} onClick={submitEvidence}>确认提交这份证据</Button></Space>}><label className="form-field">上传日志片段（TXT / LOG / JSON，UTF-8，最多64KB / 12000字符）<input aria-label="上传日志片段" type="file" accept=".txt,.log,.json" onChange={async e=>{const f=e.target.files?.[0];if(!f)return;setActionBusy(true);try{const body=new FormData();body.append('file',f);const r=await api<{text:string;filename:string}>('/api/maintenance/evidence/file-preview',{method:'POST',body});setEvidenceText(r.text);setEvidenceLabel(r.filename.slice(0,100));setPreviewed(true);}catch(error){message.error((error as Error).message);}finally{setActionBusy(false);}}}/></label>
      <Alert type="info" title="日志只作为本次会话证据，不会自动写进长期知识库。" description="先遮蔽常见密码、令牌、连接串密码和邮箱；仍需人工检查IP和其他个人信息。只有确认提交后的文本才会发送给模型。" />
      <div className="form-field"><label htmlFor="evidence-label">这是什么证据</label><Input id="evidence-label" value={evidenceLabel} maxLength={100} onChange={e => setEvidenceLabel(e.target.value)} /></div>
      <div className="form-field"><label htmlFor="evidence-time">采集时间 / 时区 / 请求范围（可选）</label><Input id="evidence-time" value={collectedAt} maxLength={80} onChange={e => setCollectedAt(e.target.value)} placeholder="例如 2026-11-11 21:00 北京时间，重试了3次下单请求" /></div>
      <div className="form-field"><label htmlFor="evidence-endpoint">对应接口（只有HTTP状态行时请填写）</label><Input id="evidence-endpoint" value={evidenceEndpoint} maxLength={200} onChange={e => setEvidenceEndpoint(e.target.value)} placeholder="/orders；日志中保留带时区时间戳，如2026-11-12T10:00:00+08:00" /></div>
      {evidenceStep !== undefined && <Tag>关联已确认方案的检查 {evidenceStep + 1}</Tag>}
      <Input.TextArea className="section-gap" aria-label="证据文本" value={evidenceText} maxLength={12000} rows={10} onChange={e => {setEvidenceText(e.target.value); setPreviewed(false);}} placeholder="粘贴相关日志、报错、脱敏配置片段，或操作后重新请求的结果…" />
      {previewed && <Alert className="section-gap" type="warning" title="上面已替换为脱敏预览。请核对无误后确认提交；编辑后需要重新预览。" />}
    </Modal>
    <Modal open={saveOpen} title="归档处理记录" onCancel={() => setSaveOpen(false)} onOk={saveCase} confirmLoading={actionBusy} okText="归档记录" okButtonProps={{disabled: !confirmed || !solution.trim()}}>
      <p>记录实际做过的操作、结果或暂停原因。归档保留当前问题状态；需要确认解决时请到问题中心操作。</p><Input.TextArea aria-label="处理记录" value={solution} maxLength={2000} rows={5} onChange={e => setSolution(e.target.value)} placeholder="我检查了什么、结果如何、哪些问题仍未确认…" /><Checkbox className="section-gap" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>我已核对这份记录，同意保留为本网站的处理参考</Checkbox>
    </Modal>
  </>;
}
