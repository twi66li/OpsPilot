import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Collapse, Empty, Input, Modal, Select, Space, Steps, Tag } from 'antd';
import { api, formatTime, post, streamChat } from './api';
import type { AgentEvent, ChatMessage, Hit, PersonalCase, Site, SupportEntry, Workflow } from './types';
import { caseStates as states, workflowNames, workflows } from './supportTypes';
import SiteEditor from './SiteEditor';
import SampleResults from './SampleResults';
import PlanChecklist from './PlanChecklist';
import ConversationList from './ConversationList';

const verdicts: Record<string, string> = {inconclusive: '证据仍不足', still_failing: '样本中仍有异常', improved: '样本显示改善', sample_recovered: '提交样本支持原症状不再复现'};

export default function PersonalSupport({entry, onSource, onKnowledge, onIssues}: {entry?: SupportEntry; onSource: (hit: Hit) => void; onKnowledge: () => void; onIssues: () => void}) {
  const {message} = App.useApp();
  const [cases, setCases] = useState<PersonalCase[]>([]), [sites, setSites] = useState<Site[]>([]);
  const [current, setCurrent] = useState<PersonalCase | null>(null), [siteId, setSiteId] = useState<string | undefined>();
  const [messages, setMessages] = useState<ChatMessage[]>([]), [question, setQuestion] = useState(''), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]), [hits, setHits] = useState<Hit[]>([]);
  const [siteOpen, setSiteOpen] = useState(false), [editingSite, setEditingSite] = useState<string | undefined>();
  const [workflow, setWorkflow] = useState<Workflow>('auto'), [evidenceEndpoint, setEvidenceEndpoint] = useState(''), [evidenceStep, setEvidenceStep] = useState<number | undefined>();
  const [evidenceOpen, setEvidenceOpen] = useState(false), [evidenceText, setEvidenceText] = useState(''), [evidenceLabel, setEvidenceLabel] = useState('应用错误日志');
  const [evidenceKind, setEvidenceKind] = useState<'initial' | 'check_result'>('initial'), [collectedAt, setCollectedAt] = useState(''), [previewed, setPreviewed] = useState(false);
  const [actionBusy, setActionBusy] = useState(false), [saveOpen, setSaveOpen] = useState(false), [solution, setSolution] = useState(''), [confirmed, setConfirmed] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    const [records, profiles] = await Promise.all([api<PersonalCase[]>('/api/support/cases'), api<Site[]>('/api/support/sites')]);
    setCases(records); setSites(profiles);
  }, []);
  useEffect(() => {refresh().catch(e => message.error(e.message)); return () => controller.current?.abort();}, [refresh, message]);
  useEffect(() => {if (entry?.caseId) {void load(entry.caseId);} else {fresh(); setSiteId(entry?.siteId); setWorkflow(entry?.workflow || 'auto'); setQuestion(entry?.question || '');}}, [entry]);
  async function load(id: string) {
    setBusy(true); setEvents([]); setWorkflow('auto'); setQuestion('');
    try {
      const [record, history] = await Promise.all([api<PersonalCase>(`/api/support/cases/${id}`), api<{messages: ChatMessage[]}>(`/api/sessions/${id}`)]);
      setCurrent(record); setSiteId(record.site_id); setMessages(history.messages); setHits(record.report_hits || []);
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  function fresh() {setCurrent(null); setMessages([]); setHits([]); setEvents([]); setQuestion(''); setWorkflow('auto');}
  async function ensureCase() {
    if (current) return current;
    const kind = workflow === 'history' ? 'history' : workflow === 'diagnose' || workflow === 'review' || /报错|异常|错误|超时|502|504|反复退出/.test(question) ? 'problem' : 'consult';
    const created = await post<PersonalCase>('/api/support/cases', {title: question.trim().slice(0, 70) || '网站维护', site_id: siteId || null, kind, symptom: kind === 'problem' ? question.trim().slice(0,2000) : ''});
    setCurrent(created); await refresh(); return created;
  }
  async function send() {
    const text = question.trim(); if (!text || busy) return;
    setBusy(true); setEvents([]); controller.current = new AbortController();
    try {
      const record = await ensureCase(); setQuestion('');
      let answer = '', runId = '';
      setMessages(previous => [...previous, {role: 'user', content: text}]);
      await streamChat(record.id, text, event => {
        if (event.run_id) runId = event.run_id;
        if (['tool_start', 'tool', 'error'].includes(event.type)) setEvents(previous => [...previous, event]);
        if (event.type === 'final') {answer = event.answer || ''; setHits(event.hits || []);}
        if (event.type === 'error') message.error(event.message || '分析未完成');
      }, controller.current.signal, workflow);
      if (answer) setMessages(previous => [...previous, {role: 'assistant', content: answer, meta: {run_id: runId}}]);
      const [updated, history] = await Promise.all([api<PersonalCase>(`/api/support/cases/${record.id}`), api<{messages: ChatMessage[]}>(`/api/sessions/${record.id}`)]);
      setCurrent(updated); setMessages(history.messages); await refresh();
    } catch (error) {if ((error as Error).name !== 'AbortError') message.error((error as Error).message);}
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
      message.success('证据已保存，点击「发送给助手」开始分析。'); await refresh();
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
  const examples = workflow === 'consult' ? ['Docker怎么查看日志？', '数据库连接池有什么作用？', '怎么理解Nginx的502和504？'] : workflow === 'history' ? ['这个网站上次遇到类似异常时怎么处理的？'] : ['网站返回502，但我还没有日志。应该先收集什么信息？', '日志出现QueuePool连接等待超时，这说明什么，下一步怎么检查？', 'Docker里的Python应用反复退出，该怎么看日志？'];
  return <div className="page-stack">
    <div className="page-title"><div><h2>智能维护助手</h2><p>咨询知识、排查异常、核对结果或查询历史。网站背景和手册均可选。</p></div><Space><Button onClick={onIssues}>管理问题记录</Button><Button disabled={busy || actionBusy} onClick={fresh}>新对话</Button></Space></div>
    {readOnly && <Alert type="info" title={current?.archived ? '当前记录已归档，原问题状态保留。' : '这个问题已由你确认解决。'} description="可以查看历史和证据；需要继续排查时，请在问题中心重新打开。" action={<Button onClick={onIssues}>前往问题中心</Button>} />}
    <div className="support-grid"><aside className="page-stack">
      <Card size="small" title="网站背景（选填）"><Select aria-label="选择网站" style={{width: '100%'}} allowClear disabled={!!current || busy} placeholder="暂不填写也能提问" value={siteId} onChange={setSiteId} options={sites.map(s => ({value: s.id, label: s.name}))} />
        <Space className="section-gap" wrap><Button size="small" disabled={!!current || busy} onClick={() => editSite(false)}>添加网站</Button><Button size="small" disabled={!siteId || !!current || busy} onClick={() => editSite(true)}>修改背景</Button></Space>
        {current && <p className="muted">本会话保留创建时的网站背景。修改背景后请开始新问题，历史依据不会随之改变。</p>}
        {current?.profile?.name && <p>{current.profile.name}<br /><small>{[current.profile.framework, current.profile.deployment, current.profile.database, current.profile.versions].filter(Boolean).join(' · ') || '技术环境尚未填写'}</small></p>}
        <Button type="link" onClick={onKnowledge}>可选：上传网站手册</Button>
      </Card>
      <Card size="small" title="维护对话与问题">{!cases.length && <p className="muted">首次提问后自动保存，之后可以继续补充结果。</p>}
        <div className="support-case-list"><ConversationList items={cases.map(c=>({id:c.id,title:c.title || '维护对话',detail:`${c.kind === 'problem' ? states[c.status] || c.status : c.kind === 'history' ? '历史查询' : '知识咨询'}${c.archived ? ' · 已归档' : ''} · ${formatTime(c.created_at)}`}))} current={current?.id} busy={busy || actionBusy} open={id=>void load(id)} deleted={async ids=>{if(current && ids.includes(current.id)){fresh();setSiteId(undefined);}await refresh();}} /></div>
      </Card>
    </aside><main className="page-stack support-main">
      <Card title={<Space>{current?.kind === 'problem' ? '当前问题' : '维护咨询'}<Tag>{workflowNames[report?.workflow || workflow] || '维护助手'}</Tag></Space>}>
        {!messages.length && <><p>例如：网站返回 502、接口一直超时，或 Python 日志出现数据库连接错误。说清楚异常和发生时间；不知道技术细节可以留空。</p>
          <Space wrap>{examples.map(text => <Button key={text} size="small" disabled={busy} onClick={() => setQuestion(text)}>{text}</Button>)}</Space></>}
        {!!messages.length && <Collapse className="section-bottom" items={[{key: 'history', label: `完整对话 · ${messages.length} 条`, children: <div className="support-history">{messages.map((m, i) => <article className={`chat-message ${m.role}`} key={i}><div className="message-label">{m.role === 'user' ? '你' : 'OpsPilot'}</div><div className="message-body">{m.content}</div></article>)}</div>}]} />}
        <Select className="section-gap" aria-label="维护任务类型" disabled={busy || readOnly} value={workflow} onChange={setWorkflow} options={workflows} style={{width:200}} />
        <Input.TextArea className="section-gap" aria-label="网站排障问题" disabled={readOnly} placeholder="可以问维护知识，也可以描述异常。日志建议先通过下面的入口脱敏后提交。" value={question} maxLength={6000} rows={3} onChange={e => setQuestion(e.target.value)} onKeyDown={e => {if (e.ctrlKey && e.key === 'Enter') void send();}} />
        <div className="composer-footer"><Space wrap><Button disabled={busy || actionBusy || readOnly} onClick={() => openEvidence('initial')}>添加日志 / 补充证据</Button><Button disabled={busy || actionBusy || readOnly || !planConfirmed} onClick={() => openEvidence('check_result')}>提交操作后结果</Button></Space>
          <Button type="primary" loading={busy} disabled={!question.trim() || actionBusy || readOnly} onClick={send}>发送给助手</Button></div>
        {busy && <Alert className="section-gap" type="info" title="正在检索与核对证据，完成后展示排障报告…" />}
        {!!events.length && <p className="muted">{events.map((e, i) => <span key={i}>{e.type === 'error' ? e.message : e.type === 'tool_start' ? '正在检索… ' : `${e.summary || '检索结束'} `}</span>)}</p>}
      </Card>
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
    </main><aside className="page-stack">
      <Card size="small" title={`本次证据 · ${current?.evidence?.length || 0}`}>{!current?.evidence?.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="可以先咨询；排查具体故障时再提交日志" />}
        <Collapse items={(current?.evidence || []).map(e => ({key: e.id, label: <>{e.label}<small> · {e.kind === 'check_result' ? '操作后结果' : '初始/补充证据'}</small></>, children: <><small>{e.id} · {e.collected_at || '未填写采集时间'}</small><pre>{e.text.split('\n').map((line, i) => `L${i + 1}: ${line}`).join('\n')}</pre></>}))} />
      </Card>
      <Card size="small" title={`资料依据 · ${hits.length}`}>{!hits.length && <p className="muted">检索后显示官方出处或你选中的网站手册。手册不是本次故障的实时证据。</p>}{hits.map(h => <button key={h.chunk_id} className="evidence-card" onClick={() => onSource(h)}><strong>[{h.citation_id}] {h.doc_title}</strong><small>{h.source_kind === 'official' ? `官方资料整理 · ${h.checked_at}` : `网站手册 · ${h.version ? `v${h.version}` : '正文'}`}</small><span>查看适用版本与原文 →</span></button>)}</Card>
      {!!current?.reports?.length && <Card size="small" title="历次判断与资料版本"><Collapse items={current.reports.map(r => ({key: String(r.revision), label: `第 ${r.revision} 次分析`, children: <><p>{r.report.summary}</p><small>{verdicts[r.report.verification.verdict]}</small>{r.hits.map(h => <Button type="link" style={{whiteSpace: 'normal', height: 'auto'}} key={h.chunk_id} onClick={() => onSource(h)}>[{h.citation_id}] {h.doc_title}{h.version ? ` v${h.version}` : ''}</Button>)}</>}))} /></Card>}
    </aside></div>
    <SiteEditor open={siteOpen} site={sites.find(s => s.id === editingSite)} close={() => setSiteOpen(false)} saved={s => {setSiteId(s.id); setSiteOpen(false); refresh().catch(e => message.error(e.message));}} />
    <Modal open={evidenceOpen} title={evidenceKind === 'check_result' ? '提交操作后的新结果' : '添加日志或检查证据'} onCancel={() => setEvidenceOpen(false)} footer={<Space><Button onClick={() => setEvidenceOpen(false)}>取消</Button><Button loading={actionBusy} disabled={!evidenceText.trim()} onClick={preview}>脱敏预览</Button><Button type="primary" disabled={!previewed || !evidenceText.trim() || !evidenceLabel.trim()} loading={actionBusy} onClick={submitEvidence}>确认提交这份证据</Button></Space>}>
      <Alert type="info" title="日志只作为本次会话证据，不会自动写进长期知识库。" description="先遮蔽常见密码、令牌、连接串密码和邮箱；仍需人工检查IP和其他个人信息。只有确认提交后的文本才会发送给模型。" />
      <div className="form-field"><label htmlFor="evidence-label">这是什么证据</label><Input id="evidence-label" value={evidenceLabel} maxLength={100} onChange={e => setEvidenceLabel(e.target.value)} /></div>
      <div className="form-field"><label htmlFor="evidence-time">采集时间 / 时区 / 请求范围（可选）</label><Input id="evidence-time" value={collectedAt} maxLength={80} onChange={e => setCollectedAt(e.target.value)} placeholder="例如 2026-11-11 21:00 北京时间，重试了3次下单请求" /></div>
      <div className="form-field"><label htmlFor="evidence-endpoint">对应接口（只有HTTP状态行时请填写）</label><Input id="evidence-endpoint" value={evidenceEndpoint} maxLength={200} onChange={e => setEvidenceEndpoint(e.target.value)} placeholder="/orders；日志中保留带时区时间戳，如2026-11-12T10:00:00+08:00" /></div>
      {evidenceStep !== undefined && <Tag>关联已确认方案的检查 {evidenceStep + 1}</Tag>}
      <input aria-label="选择日志文件" type="file" accept=".txt,.log" onChange={async e => {const file = e.target.files?.[0]; if (!file) return; if (file.size > 48000) {message.warning('请截取相关日志，文本不超过12000字符'); return;} const text = await file.text(); if (text.length > 12000) {message.warning('日志不超过12000字符'); return;} setEvidenceText(text); setPreviewed(false);}} />
      <Input.TextArea className="section-gap" aria-label="证据文本" value={evidenceText} maxLength={12000} rows={10} onChange={e => {setEvidenceText(e.target.value); setPreviewed(false);}} placeholder="粘贴相关日志、报错、脱敏配置片段，或操作后重新请求的结果…" />
      {previewed && <Alert className="section-gap" type="warning" title="上面已替换为脱敏预览。请核对无误后确认提交；编辑后需要重新预览。" />}
    </Modal>
    <Modal open={saveOpen} title="归档处理记录" onCancel={() => setSaveOpen(false)} onOk={saveCase} confirmLoading={actionBusy} okText="归档记录" okButtonProps={{disabled: !confirmed || !solution.trim()}}>
      <p>记录实际做过的操作、结果或暂停原因。归档保留当前问题状态；需要确认解决时请到问题中心操作。</p><Input.TextArea aria-label="处理记录" value={solution} maxLength={2000} rows={5} onChange={e => setSolution(e.target.value)} placeholder="我检查了什么、结果如何、哪些问题仍未确认…" /><Checkbox className="section-gap" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>我已核对这份记录，同意保留为本网站的处理参考</Checkbox>
    </Modal>
  </div>;
}
