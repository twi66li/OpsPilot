import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Drawer, Empty, Input, Modal, Select, Space, Tag } from 'antd';
import { ArrowUpOutlined, HistoryOutlined, PlusOutlined, ToolOutlined } from '@ant-design/icons';
import { api, followChat, newRequestId, post, savedAuth, streamChat } from './api';
import ConversationMenu from './ConversationMenu';
import ChatDiagnosisCard from './ChatDiagnosisCard';
import EvidenceSummary, { RedactionNotice } from './EvidenceSummary';
import { askPersonalModel, type PersonalModel } from './AssistantSettings';
import type { AgentEvent, ChatMessage, EvidencePreview, Hit, PersonalCase, SessionHistory, Site, Workflow } from './types';

const conversations = '/api/assistant/conversations';
type Thread = {id: string; title: string};
type DiagnoseOut = {
  case: PersonalCase;
  workflow: Workflow;
  prompt: string;
  redaction: NonNullable<ChatMessage['meta']>['redaction'];
};

const LOG_SIGNAL = /(\b(ERROR|WARN|WARNING|CRITICAL|FATAL|Traceback|Exception|HTTP\/\d|50[0234]|40[013489]|429|timeout)\b|超时|异常|报错|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})/i;
function looksLikeLog(text: string) {
  const trimmed = text.trim();
  return (trimmed.split('\n').length >= 3 || trimmed.length >= 140) && LOG_SIGNAL.test(trimmed);
}

export default function AssistantChat({id, open, model, configure, onSource, onIssues}: {
  id?: string;
  open: (id?: string) => void;
  model: PersonalModel | null;
  configure: () => void;
  onSource: (hit: Hit) => void;
  onIssues: (caseId: string) => void;
}) {
  const {message} = App.useApp();
  const guest = !!savedAuth()?.is_guest;
  const [threads, setThreads] = useState<Thread[]>([]);
  const [sites, setSites] = useState<Site[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cases, setCases] = useState<Record<string, PersonalCase>>({});
  const [question, setQuestion] = useState('');
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState('');
  const [diagCaseId, setDiagCaseId] = useState('');
  const [diagPhase, setDiagPhase] = useState('');
  const [diagRunId, setDiagRunId] = useState('');
  const [diagModalOpen, setDiagModalOpen] = useState(false);
  const [targetCaseId, setTargetCaseId] = useState<string | undefined>();
  const [stepIndex, setStepIndex] = useState<number | undefined>();
  const [siteId, setSiteId] = useState<string | undefined>();
  const [evLabel, setEvLabel] = useState('日志片段');
  const [evText, setEvText] = useState('');
  const [evNote, setEvNote] = useState('');
  const [evPreview, setEvPreview] = useState<EvidencePreview | null>(null);
  const [modalBusy, setModalBusy] = useState(false);
  const current = useRef<string | undefined>(undefined);
  const controller = useRef<AbortController | null>(null);
  const retry = useRef<{id: string; text: string; direct: PersonalModel | null; answer?: string} | null>(null);
  const timeline = useRef<HTMLDivElement>(null);
  const title = threads.find(thread => thread.id === id)?.title;

  useEffect(() => {
    const request = new AbortController();
    api<Thread[]>(conversations, {signal: request.signal}).then(setThreads).catch(e => {
      if (!request.signal.aborted) setError(e.message);
    });
    if (!guest) {
      api<Site[]>('/api/support/sites', {signal: request.signal}).then(setSites).catch(() => {});
    }
    return () => {request.abort(); controller.current?.abort();};
  }, [guest]);

  useEffect(() => {
    if (current.current === id) return;
    current.current = id;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setMessages([]); setCases({}); setDraft(''); setQuestion(''); setError(''); setPending('');
    setRunId(''); setDiagCaseId(''); setDiagPhase(''); setDiagRunId('');
    retry.current = null;
    setBusy(!!id);
    if (id) void read(id, request.signal, true)
      .catch(e => {if (!request.signal.aborted) setError(e.message);})
      .finally(() => {if (!request.signal.aborted) setBusy(false);});
  }, [id]);

  useEffect(() => {
    if (timeline.current) timeline.current.scrollTop = timeline.current.scrollHeight;
  }, [messages, pending, draft, diagPhase]);

  function events(event: AgentEvent) {
    if (event.run_id) setRunId(event.run_id);
    if (event.type === 'answer_reset') setDraft('');
    if (event.type === 'token') setDraft(previous => previous + (event.delta || ''));
    if (event.type === 'error') setError(event.message || '回复失败，请稍后重试');
  }

  function diagEvents(event: AgentEvent) {
    if (event.run_id) setDiagRunId(event.run_id);
    if (event.type === 'phase') setDiagPhase(event.message || '正在分析证据…');
    if (event.type === 'tool_start') setDiagPhase('正在检索相关资料…');
    if (event.type === 'tool') setDiagPhase('已取得资料，正在整理结论…');
    if (event.type === 'error') setError(event.message || '诊断未完成，证据已保留');
  }

  async function refreshCase(caseId: string, signal?: AbortSignal) {
    const saved = await api<PersonalCase>('/api/support/cases/' + caseId, signal ? {signal} : undefined);
    if (signal?.aborted) return;
    setCases(prev => ({...prev, [caseId]: saved}));
  }

  async function read(threadId: string, signal: AbortSignal, resume = false) {
    const result = await api<SessionHistory>(conversations + '/' + threadId, {signal});
    if (signal.aborted) return;
    setMessages(result.messages);
    const loadedCases = result.cases || {};
    setCases(loadedCases);
    if (resume && result.active_run_id) {
      setRunId(result.active_run_id);
      await followChat(result.active_run_id, events, signal);
      await read(threadId, signal);
      return;
    }
    if (resume) {
      const activeDiag = Object.values(loadedCases).find(c => c.active_run_id);
      if (activeDiag?.active_run_id) {
        setDiagCaseId(activeDiag.id);
        setDiagRunId(activeDiag.active_run_id);
        setDiagPhase('正在继续后台诊断…');
        await followChat(activeDiag.active_run_id, diagEvents, signal);
        await refreshCase(activeDiag.id, signal);
        setDiagCaseId(''); setDiagRunId(''); setDiagPhase('');
      }
    }
  }

  async function ensureThread(seedTitle: string, signal: AbortSignal) {
    if (id) return id;
    const thread = await api<Thread>(conversations, {method: 'POST', body: JSON.stringify({title: seedTitle.slice(0, 70) || '新对话'}), signal});
    current.current = thread.id;
    setThreads(previous => [thread, ...previous]);
    open(thread.id);
    return thread.id;
  }

  async function send() {
    const text = question.trim();
    if (!text || busy || !!diagCaseId) return;
    if (model?.key && text.includes(model.key)) {
      setError('消息包含当前模型凭据，请移除后再发送。');
      return;
    }
    const request = new AbortController();
    controller.current = request;
    const {signal} = request;
    const turn = retry.current?.text === text && retry.current.direct === model
      ? retry.current : {id: newRequestId(), text, direct: model};
    retry.current = turn;
    setBusy(true); setError(''); setDraft(''); setRunId(''); setPending(text); setQuestion('');
    try {
      const threadId = await ensureThread(text, signal);
      if (signal.aborted) return;
      if (turn.direct) {
        const url = conversations + '/' + threadId + '/personal-turn';
        const requestId = {request_id: turn.id, message: text};
        const started = await api<{answer: string | null}>(url, {method: 'POST', body: JSON.stringify(requestId), signal});
        if (!started.answer) {
          const context = messages.filter(m => (m.role === 'assistant' || m.role === 'user') && !m.meta?.case_id).slice(-10)
            .map(m => ({role: m.role, content: m.content.slice(0, 8000)}));
          turn.answer ??= await askPersonalModel(turn.direct, [...context, {role: 'user', content: text}], signal);
          await api(url, {method: 'POST', body: JSON.stringify({...requestId, answer: turn.answer}), signal});
        }
      } else {
        let failed = false;
        await streamChat(threadId, text, event => {
          if (event.type === 'error') failed = true;
          events(event);
        }, signal, 'chat', turn.id);
        if (failed) {
          retry.current = null;
          setQuestion(text);
        }
      }
      await read(threadId, signal);
      retry.current = null;
    } catch (e) {
      if (signal.aborted) return;
      setError((e as Error).message);
      setQuestion(text);
    } finally {
      if (!signal.aborted) {setBusy(false); setDraft(''); setPending(''); setRunId('');}
    }
  }

  function activeCaseInThread() {
    for (let i = messages.length - 1; i >= 0; i--) {
      const cid = messages[i].meta?.case_id;
      const item = cid ? cases[cid] : undefined;
      if (item && !item.archived && item.status !== 'user_resolved') return item;
    }
    return undefined;
  }

  function openDiagnoseModal(caseId?: string, step?: number, prefillText = '') {
    if (guest) {
      message.info('登录个人账号后可提交日志进行证据化诊断');
      return;
    }
    const existing = caseId ? cases[caseId] : activeCaseInThread();
    setTargetCaseId(existing?.id);
    setStepIndex(step);
    setSiteId(existing?.site_id);
    setEvLabel(existing?.plan ? '复测结果' : '日志片段');
    setEvText(prefillText);
    setEvNote('');
    setEvPreview(null);
    setDiagModalOpen(true);
  }

  async function previewRedaction() {
    if (!evText.trim()) return;
    setModalBusy(true);
    try {
      const target = targetCaseId ? cases[targetCaseId] : undefined;
      const res = await post<EvidencePreview>('/api/support/evidence/preview', {
        text: evText,
        label: evLabel,
        kind: target?.plan ? 'check_result' : 'initial',
      });
      setEvText(res.text);
      setEvPreview(res);
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setModalBusy(false);
    }
  }

  async function startDiagnosis(payload: {text: string; label: string; question?: string; caseId?: string; siteId?: string; step?: number; clearComposer?: boolean}) {
    const raw = payload.text.trim();
    if (!raw || busy || !!diagCaseId) return;
    setModalBusy(true);
    setError('');
    const request = new AbortController();
    controller.current = request;
    const {signal} = request;
    let activeCase = '';
    try {
      const threadId = await ensureThread(payload.question || payload.label || raw.split('\n')[0], signal);
      if (signal.aborted) return;
      const prepared = await api<DiagnoseOut>(`${conversations}/${threadId}/diagnose`, {
        method: 'POST',
        body: JSON.stringify({
          request_id: newRequestId(),
          text: raw.slice(0, 12000),
          label: payload.label.trim() || '日志片段',
          question: payload.question?.trim() || '',
          case_id: payload.caseId || null,
          site_id: payload.siteId || null,
          step_index: payload.step ?? null,
        }),
        signal,
      });
      if (payload.clearComposer) setQuestion('');
      setDiagModalOpen(false);
      setModalBusy(false);
      activeCase = prepared.case.id;
      setDiagCaseId(activeCase);
      setDiagPhase('正在结合证据诊断…');
      setDiagRunId('');
      await read(threadId, signal);
      await streamChat(activeCase, prepared.prompt, diagEvents, signal, prepared.workflow, newRequestId());
      await refreshCase(activeCase, signal);
    } catch (e) {
      if (!signal.aborted) setError((e as Error).message);
    } finally {
      setModalBusy(false);
      if (!signal.aborted) {
        setDiagCaseId('');
        setDiagPhase('');
        setDiagRunId('');
      }
    }
  }

  function choose(threadId?: string) {
    setHistoryOpen(false);
    if (threadId === id && !id) {setQuestion(''); setError(''); retry.current = null;}
    open(threadId);
  }

  const working = busy || !!diagCaseId;
  const pasteLooksLikeLog = !guest && !working && looksLikeLog(question);
  const lastIndexByCase = new Map<string, number>();
  messages.forEach((m, idx) => {
    if (m.meta?.case_id) lastIndexByCase.set(m.meta.case_id, idx);
  });

  const list = <><div className="support-list-heading"><strong>对话</strong>
    <Button size="small" icon={<PlusOutlined aria-hidden="true" />} onClick={() => choose()} disabled={working}>新对话</Button></div>
    <div className="support-case-list">
      {!threads.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话" />}
      {threads.map(thread => <div className="conversation-row" key={thread.id}>
        <button className={`session-item ${id === thread.id ? 'selected' : ''}`} disabled={working} onClick={() => choose(thread.id)}>{thread.title}</button>
        <ConversationMenu title={thread.title} path={conversations + '/' + thread.id} disabled={working}
          renamed={name => setThreads(previous => previous.map(item => item.id === thread.id ? {...item, title: name} : item))}
          deleted={() => {setThreads(previous => previous.filter(item => item.id !== thread.id)); if (id === thread.id) choose();}} />
      </div>)}
    </div></>;

  function bubble(role: string, content: string, key: string | number) {
    return <article className={`support-message ${role}`} key={key}><div className="support-bubble"><div className="message-body">{content}</div></div></article>;
  }

  const modalCase = targetCaseId ? cases[targetCaseId] : undefined;

  return <div className="support-shell plain-assistant">
    <aside className="support-conversations">{list}</aside>
    <Drawer className="assistant-history-drawer" placement="left" title="对话历史" size={300}
      open={historyOpen} onClose={() => setHistoryOpen(false)}>{list}</Drawer>
    <div className="support-chat">
      <div className="support-chat-top">
        <Space className="support-heading-actions"><Button className="assistant-mobile-history" type="text" icon={<HistoryOutlined />} aria-label="对话历史" onClick={() => setHistoryOpen(true)} />
          <h3 title={title}>{title || '新对话'}</h3></Space>
        <Button type="text" onClick={configure}>{model ? '自带模型' : '模型设置'}</Button>
      </div>
      <div className="support-chat-scroll" ref={timeline} role="log" aria-label="聊天记录" aria-live="polite">
        {!messages.length && !pending && !working && <div className="plain-assistant-greeting"><h2>今天想聊点什么？</h2></div>}
        {messages.map((item, i) => {
          const cid = item.meta?.case_id;
          if (cid) {
            if (lastIndexByCase.get(cid) !== i) return null;
            const record = cases[cid];
            if (!record) return bubble('assistant', '诊断记录加载中…', i);
            const isRunning = diagCaseId === cid;
            return (
              <div className="support-report-flow" key={i}>
                <ChatDiagnosisCard
                  record={record}
                  redaction={item.meta?.redaction}
                  running={isRunning}
                  phase={isRunning ? diagPhase : undefined}
                  disabled={guest || working}
                  onStop={isRunning && diagRunId ? () => void post('/api/agent/runs/' + diagRunId + '/cancel').catch(e => setError(e.message)) : undefined}
                  onAddEvidence={step => openDiagnoseModal(cid, step)}
                  onChanged={next => {
                    if (next) setCases(prev => ({...prev, [cid]: next}));
                    else void refreshCase(cid);
                  }}
                  onSource={onSource}
                  onDetail={onIssues}
                />
              </div>
            );
          }
          return bubble(item.role, item.content, i);
        })}
        {pending && bubble('user', pending, 'pending')}
        {busy && bubble('assistant', draft || '正在回复…', 'reply')}
      </div>
      <div className="support-composer">
        {error && <div className="assistant-inline-error" role="alert">{error}<Button type="text" size="small" onClick={() => setError('')}>关闭</Button></div>}
        {pasteLooksLikeLog && (
          <div className="assistant-log-banner" role="region" aria-label="日志诊断提示">
            <span>检测到日志或报错片段，可直接脱敏诊断</span>
            <Button
              size="small"
              type="primary"
              onClick={() => void startDiagnosis({
                text: question,
                label: activeCaseInThread()?.plan ? '复测结果' : '粘贴的日志片段',
                caseId: activeCaseInThread()?.id,
                clearComposer: true,
              })}
            >
              作为证据诊断
            </Button>
          </div>
        )}
        <div className="support-composer-box">
          <textarea aria-label="给维护助手发消息" placeholder="发消息提问，或直接粘贴报错日志触发诊断" value={question}
            rows={2} maxLength={8000} disabled={working} onChange={e => setQuestion(e.target.value)} onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); void send();}
            }} />
          <div className="support-composer-tools">
            <Button
              size="small"
              icon={<ToolOutlined aria-hidden="true" />}
              onClick={() => openDiagnoseModal(undefined, undefined, looksLikeLog(question) ? question : '')}
              disabled={working}
            >
              诊断
            </Button>
            {busy && runId ? <Button size="small" onClick={() => void post('/api/agent/runs/' + runId + '/cancel').catch(e => setError(e.message))}>停止生成</Button>
              : <Button aria-label="发送" title="发送" type="primary" icon={<ArrowUpOutlined />} disabled={working || !question.trim()} onClick={() => void send()} />}
          </div>
        </div>
      </div>
    </div>

    <Modal
      open={diagModalOpen}
      title={modalCase?.plan ? '补充复测结果' : '提交日志或证据开始诊断'}
      onCancel={() => setDiagModalOpen(false)}
      footer={
        <Space>
          <Button onClick={() => setDiagModalOpen(false)}>取消</Button>
          <Button loading={modalBusy} disabled={!evText.trim()} onClick={previewRedaction}>脱敏预览</Button>
          <Button
            type="primary"
            loading={modalBusy}
            disabled={!evText.trim() || !evLabel.trim()}
            onClick={() => void startDiagnosis({
              text: evText,
              label: evLabel,
              question: evNote,
              caseId: targetCaseId,
              siteId,
              step: stepIndex,
              clearComposer: evText.trim() === question.trim(),
            })}
          >
            提交并诊断
          </Button>
        </Space>
      }
    >
      <Alert
        type="info"
        title="提交时自动遮蔽常见密码、令牌和邮箱；请留意日志中的公网 IP 等信息。"
      />
      {!targetCaseId && !!sites.length && (
        <div className="form-field section-gap">
          <label>关联网站（选填）</label>
          <Select
            aria-label="诊断关联网站"
            allowClear
            style={{width: '100%'}}
            placeholder="选择网站以结合技术背景与私人手册"
            value={siteId}
            onChange={setSiteId}
            options={sites.map(s => ({value: s.id, label: s.name}))}
          />
        </div>
      )}
      {stepIndex !== undefined && <Tag className="section-gap">关联已确认方案的检查 {stepIndex + 1}</Tag>}
      <label className="form-field section-gap">
        上传日志文件（TXT / LOG / JSON，最多 64KB）
        <input
          aria-label="上传日志片段"
          type="file"
          accept=".txt,.log,.json"
          onChange={async e => {
            const f = e.target.files?.[0];
            if (!f) return;
            setModalBusy(true);
            try {
              const body = new FormData();
              body.append('file', f);
              const r = await api<EvidencePreview>('/api/maintenance/evidence/file-preview', {method: 'POST', body});
              setEvText(r.text);
              setEvLabel((r.filename || f.name).slice(0, 100));
              setEvPreview(r);
            } catch (err) {
              message.error((err as Error).message);
            } finally {
              setModalBusy(false);
            }
          }}
        />
      </label>
      <div className="form-field">
        <label htmlFor="chat-evidence-label">证据名称</label>
        <Input id="chat-evidence-label" value={evLabel} maxLength={100} onChange={e => setEvLabel(e.target.value)} />
      </div>
      <Input.TextArea
        className="section-gap"
        aria-label="证据文本"
        value={evText}
        maxLength={12000}
        rows={8}
        onChange={e => {setEvText(e.target.value); setEvPreview(null);}}
        placeholder="粘贴报错堆栈、访问日志，或操作后的检查输出…"
      />
      <Input
        className="section-gap"
        aria-label="补充说明"
        value={evNote}
        maxLength={500}
        onChange={e => setEvNote(e.target.value)}
        placeholder="补充说明（选填，例如发生时间或刚做过的操作）"
      />
      {evPreview && (
        <div className="section-gap">
          <RedactionNotice value={evPreview} />
          {evPreview.summary && <EvidenceSummary value={evPreview.summary} />}
        </div>
      )}
    </Modal>
  </div>;
}
