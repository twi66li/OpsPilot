import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Collapse, Empty, Form, Input, Modal, Select, Space, Steps, Tag } from 'antd';
import { api, formatTime, post, streamChat } from './api';
import type { AgentEvent, ChatMessage, Hit, KnowledgeDoc, PersonalCase, Site } from './types';

const states: Record<string, string> = {investigating: '逐步排查中', awaiting_results: '等待你提交结果', evidence_submitted: '新证据待分析', sample_recovered: '提交样本支持改善', saved: '已保存处理案例'};
const verdicts: Record<string, string> = {inconclusive: '证据仍不足', still_failing: '样本中仍有异常', improved: '样本显示改善', sample_recovered: '提交样本支持原症状不再复现'};

export default function PersonalSupport({onSource, onKnowledge}: {onSource: (hit: Hit) => void; onKnowledge: () => void}) {
  const {message} = App.useApp();
  const [cases, setCases] = useState<PersonalCase[]>([]), [sites, setSites] = useState<Site[]>([]), [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [current, setCurrent] = useState<PersonalCase | null>(null), [siteId, setSiteId] = useState<string | undefined>();
  const [messages, setMessages] = useState<ChatMessage[]>([]), [question, setQuestion] = useState(''), [busy, setBusy] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]), [hits, setHits] = useState<Hit[]>([]);
  const [siteOpen, setSiteOpen] = useState(false), [editingSite, setEditingSite] = useState<string | undefined>(), [siteForm] = Form.useForm();
  const [evidenceOpen, setEvidenceOpen] = useState(false), [evidenceText, setEvidenceText] = useState(''), [evidenceLabel, setEvidenceLabel] = useState('应用错误日志');
  const [evidenceKind, setEvidenceKind] = useState<'initial' | 'check_result'>('initial'), [collectedAt, setCollectedAt] = useState(''), [previewed, setPreviewed] = useState(false);
  const [actionBusy, setActionBusy] = useState(false), [saveOpen, setSaveOpen] = useState(false), [solution, setSolution] = useState(''), [confirmed, setConfirmed] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    const [records, profiles, privateDocs] = await Promise.all([api<PersonalCase[]>('/api/support/cases'), api<Site[]>('/api/support/sites'), api<KnowledgeDoc[]>('/api/support/private-documents')]);
    setCases(records); setSites(profiles); setDocs(privateDocs);
  }, []);
  useEffect(() => {refresh().catch(e => message.error(e.message)); return () => controller.current?.abort();}, [refresh, message]);
  async function load(id: string) {
    setBusy(true); setEvents([]);
    try {
      const [record, history] = await Promise.all([api<PersonalCase>(`/api/support/cases/${id}`), api<{messages: ChatMessage[]}>(`/api/sessions/${id}`)]);
      setCurrent(record); setSiteId(record.site_id); setMessages(history.messages); setHits(record.report_hits || []);
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  function fresh() {setCurrent(null); setMessages([]); setHits([]); setEvents([]); setQuestion('');}
  async function ensureCase() {
    if (current) return current;
    const created = await post<PersonalCase>('/api/support/cases', {title: question.trim().slice(0, 70) || '网站排障', site_id: siteId || null});
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
      }, controller.current.signal);
      if (answer) setMessages(previous => [...previous, {role: 'assistant', content: answer, meta: {run_id: runId}}]);
      const [updated, history] = await Promise.all([api<PersonalCase>(`/api/support/cases/${record.id}`), api<{messages: ChatMessage[]}>(`/api/sessions/${record.id}`)]);
      setCurrent(updated); setMessages(history.messages); await refresh();
    } catch (error) {if ((error as Error).name !== 'AbortError') message.error((error as Error).message);}
    finally {setBusy(false);}
  }
  function editSite(edit: boolean) {
    const site = edit ? sites.find(s => s.id === siteId) : undefined;
    setEditingSite(site?.id); siteForm.resetFields();
    siteForm.setFieldsValue(site || {name: '我的网站', doc_ids: []}); setSiteOpen(true);
  }
  async function saveSite() {
    try {
      const values = await siteForm.validateFields(); setActionBusy(true);
      const saved = await api<Site>(editingSite ? `/api/support/sites/${editingSite}` : '/api/support/sites', {method: editingSite ? 'PUT' : 'POST', body: JSON.stringify(values)});
      setSiteId(saved.id); setSiteOpen(false); await refresh();
      message.success('网站背景已保存，新会话将使用这份背景。');
    } catch (error) {if (error instanceof Error) message.error(error.message);} finally {setActionBusy(false);}
  }
  function openEvidence(kind: 'initial' | 'check_result') {
    setEvidenceKind(kind); setEvidenceText(''); setEvidenceLabel(kind === 'check_result' ? '操作后的检查结果' : '应用错误日志');
    setCollectedAt(''); setPreviewed(false); setEvidenceOpen(true);
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
      await post(`/api/support/cases/${record.id}/evidence`, {text: evidenceText, label: evidenceLabel, kind: evidenceKind, collected_at: collectedAt});
      setCurrent(await api<PersonalCase>(`/api/support/cases/${record.id}`)); setEvidenceOpen(false); setEvidenceText('');
      if (!question.trim()) setQuestion(evidenceKind === 'check_result' ? '我已提交新的检查结果，请结合之前确认的方案核对原问题是否仍存在，还有什么证据不足？' : '请结合我提交的日志和网站背景，解释异常，并给出下一步最需要做的检查。');
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
  return <div className="page-stack">
    <div className="page-title"><div><h2>我的网站排障</h2><p>描述异常 → 按步骤检查 → 带回结果。没有手册也能开始；不会连接或操作你的服务器。</p></div><Button disabled={busy || actionBusy} onClick={fresh}>新问题</Button></div>
    <div className="support-grid"><aside className="page-stack">
      <Card size="small" title="网站背景（选填）"><Select aria-label="选择网站" style={{width: '100%'}} allowClear disabled={!!current || busy} placeholder="暂不填写也能提问" value={siteId} onChange={setSiteId} options={sites.map(s => ({value: s.id, label: s.name}))} />
        <Space className="section-gap" wrap><Button size="small" disabled={!!current || busy} onClick={() => editSite(false)}>添加网站</Button><Button size="small" disabled={!siteId || !!current || busy} onClick={() => editSite(true)}>修改背景</Button></Space>
        {current && <p className="muted">本会话保留创建时的网站背景。修改背景后请开始新问题，历史依据不会随之改变。</p>}
        {current?.profile?.name && <p>{current.profile.name}<br /><small>{[current.profile.framework, current.profile.deployment, current.profile.database, current.profile.versions].filter(Boolean).join(' · ') || '技术环境尚未填写'}</small></p>}
        <Button type="link" onClick={onKnowledge}>可选：上传网站手册</Button>
      </Card>
      <Card size="small" title="我的问题与已保存案例">{!cases.length && <p className="muted">首次提问后自动保存，之后可以继续补充结果。</p>}
        <div className="support-case-list">{cases.map(c => <button key={c.id} disabled={busy || actionBusy} className={`session-item ${current?.id === c.id ? 'selected' : ''}`} onClick={() => load(c.id)}>{c.title}<small>{states[c.status] || c.status} · {formatTime(c.created_at)}</small></button>)}</div>
      </Card>
    </aside><main className="page-stack support-main">
      <Card title={current ? <Space>当前问题<Tag>{states[current.status] || current.status}</Tag></Space> : '从一个具体问题开始'}>
        {!messages.length && <><p>例如：网站返回 502、接口一直超时，或 Python 日志出现数据库连接错误。说清楚异常和发生时间；不知道技术细节可以留空。</p>
          <Space wrap>{['网站返回502，但我还没有日志。应该先收集什么信息？', '日志出现QueuePool连接等待超时，这说明什么，下一步怎么检查？', 'Docker里的Python应用反复退出，该怎么看日志？'].map(text => <Button key={text} size="small" disabled={busy} onClick={() => setQuestion(text)}>{text}</Button>)}</Space></>}
        {!!messages.length && <Collapse className="section-bottom" items={[{key: 'history', label: `完整对话 · ${messages.length} 条`, children: <div className="support-history">{messages.map((m, i) => <article className={`chat-message ${m.role}`} key={i}><div className="message-label">{m.role === 'user' ? '你' : 'OpsPilot'}</div><div className="message-body">{m.content}</div></article>)}</div>}]} />}
        <Input.TextArea className="section-gap" aria-label="网站排障问题" placeholder="描述异常、发生时间和刚才检查到的结果… 日志建议通过下面的入口脱敏后提交。" value={question} maxLength={6000} rows={3} onChange={e => setQuestion(e.target.value)} onKeyDown={e => {if (e.ctrlKey && e.key === 'Enter') void send();}} />
        <div className="composer-footer"><Space wrap><Button disabled={busy || actionBusy} onClick={() => openEvidence('initial')}>添加日志 / 补充证据</Button><Button disabled={busy || actionBusy || !planConfirmed} onClick={() => openEvidence('check_result')}>提交操作后结果</Button></Space>
          <Button type="primary" loading={busy} disabled={!question.trim() || actionBusy || current?.saved} onClick={send}>发送给助手</Button></div>
        {busy && <Alert className="section-gap" type="info" title="正在检索与核对证据，完成后展示排障报告…" />}
        {!!events.length && <p className="muted">{events.map((e, i) => <span key={i}>{e.type === 'error' ? e.message : e.type === 'tool_start' ? '正在检索… ' : `${e.summary || '检索结束'} `}</span>)}</p>}
      </Card>
      {report && <>
        <Card title="当前判断"><p className="support-summary">{report.summary}</p><h4>日志直接说明了什么</h4>
          {!report.observations.length && <p className="muted">尚未提供可核对的日志行，当前只有基础指导。</p>}
          {report.observations.map((o, i) => <div key={i}><Tag>{o.evidence_id} L{o.line_start}–L{o.line_end}</Tag><p>{o.meaning}</p><pre className="source-text">{o.quote}</pre></div>)}
          {!!report.hypotheses.length && <><h4>待验证的可能原因</h4><ul>{report.hypotheses.map((h, i) => <li key={i}>{h}</li>)}</ul></>}
          {!!report.missing_info.length && <Alert type="info" title="下一步最需要补充" description={<ul>{report.missing_info.map((g, i) => <li key={i}>{g}</li>)}</ul>} />}
        </Card>
        {!!report.steps.length && <Card title="建议检查步骤（由你在自己的环境操作）">{report.steps.map((s, i) => <div className="check-step" key={i}><h4><Tag color={s.risk === '只读' ? 'green' : 'orange'}>{s.risk}</Tag>第 {i + 1} 步 · {s.location}</h4><pre className="source-text">{s.action}</pre><p><strong>正常时：</strong>{s.normal}<br /><strong>异常时：</strong>{s.abnormal}</p>{s.rollback && <Alert type="warning" title="影响与回退" description={s.rollback} />}</div>)}
          <Button disabled={busy || actionBusy || current?.plan?.revision === current?.revision} onClick={confirmPlan}>我确认检查方案，准备自行操作</Button><p className="muted">此按钮只记录你的确认，不会执行命令、修改配置或标记恢复。</p>
        </Card>}
        <Card title="处理结果核对"><Steps size="small" current={current?.saved ? 3 : current?.status === 'sample_recovered' ? 2 : planConfirmed ? 1 : 0} items={[{title: '确认方案'}, {title: '提交新结果'}, {title: '核对证据'}, {title: '保存案例'}]} />
          <Alert className="section-gap" type={report.verification.verdict === 'sample_recovered' ? 'success' : 'info'} title={verdicts[report.verification.verdict]} description={report.verification.reason} />
          <p className="muted">判断仅针对你提交的样本，不能替代线上持续监控；“已操作”不等于恢复已验证。</p><Space wrap><Button disabled={busy || actionBusy || !planConfirmed} onClick={() => openEvidence('check_result')}>添加新的检查结果</Button><Button disabled={busy || actionBusy} onClick={() => {setSolution(current?.solution || ''); setConfirmed(false); setSaveOpen(true);}}>确认并保存处理案例</Button></Space>
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
    <Modal open={siteOpen} title={editingSite ? '修改网站背景' : '添加网站背景（可选）'} onCancel={() => setSiteOpen(false)} onOk={saveSite} confirmLoading={actionBusy} okText="保存背景">
      <p>不知道的项目可以留空，不需要提供账号、密码或服务器访问权限。</p><Form form={siteForm} layout="vertical">
        <Form.Item name="name" label="网站名称" rules={[{required: true}]}><Input maxLength={80} /></Form.Item>
        <Form.Item name="framework" label="框架"><Input maxLength={120} placeholder="例如 FastAPI / Django；不知道可留空" /></Form.Item>
        <Form.Item name="deployment" label="部署方式"><Input maxLength={120} placeholder="例如 Docker Compose / Linux 直接部署" /></Form.Item>
        <Form.Item name="database" label="数据库"><Input maxLength={120} placeholder="例如 PostgreSQL / MySQL" /></Form.Item>
        <Form.Item name="versions" label="已知软件版本"><Input maxLength={200} placeholder="例如 Python 3.12、PostgreSQL 16" /></Form.Item>
        <Form.Item name="notes" label="补充说明"><Input.TextArea maxLength={1200} rows={2} placeholder="用途、近期变更等，请勿填写秘密" /></Form.Item>
        <Form.Item name="doc_ids" label="关联自己上传的手册（可选）"><Select mode="multiple" options={docs.map(d => ({value: d.id, label: d.title}))} placeholder="只选与这个网站有关的手册" /></Form.Item>
      </Form>
    </Modal>
    <Modal open={evidenceOpen} title={evidenceKind === 'check_result' ? '提交操作后的新结果' : '添加日志或检查证据'} onCancel={() => setEvidenceOpen(false)} footer={<Space><Button onClick={() => setEvidenceOpen(false)}>取消</Button><Button loading={actionBusy} disabled={!evidenceText.trim()} onClick={preview}>脱敏预览</Button><Button type="primary" disabled={!previewed || !evidenceText.trim() || !evidenceLabel.trim()} loading={actionBusy} onClick={submitEvidence}>确认提交这份证据</Button></Space>}>
      <Alert type="info" title="日志只作为本次会话证据，不会自动写进长期知识库。" description="先遮蔽常见密码、令牌、连接串密码和邮箱；仍需人工检查IP和其他个人信息。只有确认提交后的文本才会发送给模型。" />
      <div className="form-field"><label htmlFor="evidence-label">这是什么证据</label><Input id="evidence-label" value={evidenceLabel} maxLength={100} onChange={e => setEvidenceLabel(e.target.value)} /></div>
      <div className="form-field"><label htmlFor="evidence-time">采集时间 / 时区 / 请求范围（可选）</label><Input id="evidence-time" value={collectedAt} maxLength={80} onChange={e => setCollectedAt(e.target.value)} placeholder="例如 2026-11-11 21:00 北京时间，重试了3次下单请求" /></div>
      <input aria-label="选择日志文件" type="file" accept=".txt,.log" onChange={async e => {const file = e.target.files?.[0]; if (!file) return; if (file.size > 48000) {message.warning('请截取相关日志，文本不超过12000字符'); return;} const text = await file.text(); if (text.length > 12000) {message.warning('日志不超过12000字符'); return;} setEvidenceText(text); setPreviewed(false);}} />
      <Input.TextArea className="section-gap" aria-label="证据文本" value={evidenceText} maxLength={12000} rows={10} onChange={e => {setEvidenceText(e.target.value); setPreviewed(false);}} placeholder="粘贴相关日志、报错、脱敏配置片段，或操作后重新请求的结果…" />
      {previewed && <Alert className="section-gap" type="warning" title="上面已替换为脱敏预览。请核对无误后确认提交；编辑后需要重新预览。" />}
    </Modal>
    <Modal open={saveOpen} title="保存本网站的处理案例" onCancel={() => setSaveOpen(false)} onOk={saveCase} confirmLoading={actionBusy} okText="保存案例" okButtonProps={{disabled: !confirmed || !solution.trim()}}>
      <p>记录实际做过的操作和结果，不把猜测写成已确认根因。证据不足也可以保存，但会保留当前核对状态。</p><Input.TextArea aria-label="处理记录" value={solution} maxLength={2000} rows={5} onChange={e => setSolution(e.target.value)} placeholder="我检查了什么、改了什么、如何验证、哪些问题仍未确认…" /><Checkbox className="section-gap" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>我已核对这份记录，并同意作为同一网站的历史排障参考</Checkbox>
    </Modal>
  </div>;
}
