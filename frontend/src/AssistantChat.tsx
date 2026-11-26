import { useEffect, useRef, useState } from 'react';
import { Button, Drawer, Empty, Space } from 'antd';
import { ArrowUpOutlined, HistoryOutlined, PlusOutlined, ToolOutlined } from '@ant-design/icons';
import { api, followChat, newRequestId, post, streamChat } from './api';
import ConversationMenu from './ConversationMenu';
import { askPersonalModel, type PersonalModel } from './AssistantSettings';
import type { AgentEvent, ChatMessage, SessionHistory } from './types';

const conversations = '/api/assistant/conversations';
type Thread = {id: string; title: string};

export default function AssistantChat({id, open, diagnose, model, configure}: {
  id?: string; open: (id?: string) => void; diagnose: () => void; model: PersonalModel | null; configure: () => void;
}) {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState('');
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState('');
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
    return () => {request.abort(); controller.current?.abort();};
  }, []);
  useEffect(() => {
    if (current.current === id) return;
    current.current = id;
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setMessages([]); setDraft(''); setQuestion(''); setError(''); setPending(''); setRunId('');
    retry.current = null;
    setBusy(!!id);
    if (id) void read(id, request.signal, true)
      .catch(e => {if (!request.signal.aborted) setError(e.message);})
      .finally(() => {if (!request.signal.aborted) setBusy(false);});
  }, [id]);
  useEffect(() => {
    if (timeline.current) timeline.current.scrollTop = timeline.current.scrollHeight;
  }, [messages, pending, draft]);

  function events(event: AgentEvent) {
    if (event.run_id) setRunId(event.run_id);
    if (event.type === 'answer_reset') setDraft('');
    if (event.type === 'token') setDraft(previous => previous + (event.delta || ''));
    if (event.type === 'error') setError(event.message || '回复失败，请稍后重试');
  }

  async function read(threadId: string, signal: AbortSignal, resume = false) {
    const result = await api<SessionHistory>(conversations + '/' + threadId, {signal});
    if (signal.aborted) return;
    setMessages(result.messages);
    if (resume && result.active_run_id) {
      setRunId(result.active_run_id);
      await followChat(result.active_run_id, events, signal);
      await read(threadId, signal);
    }
  }

  async function send() {
    const text = question.trim();
    if (!text || busy) return;
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
    let threadId = id;
    try {
      if (!threadId) {
        const thread = await api<Thread>(conversations, {method: 'POST', body: JSON.stringify({title: text.slice(0, 70)}), signal});
        if (signal.aborted) return;
        threadId = thread.id;
        current.current = threadId;
        setThreads(previous => [thread, ...previous]);
        open(threadId);
      }
      if (turn.direct) {
        const url = conversations + '/' + threadId + '/personal-turn';
        const requestId = {request_id: turn.id, message: text};
        const started = await api<{answer: string | null}>(url, {method: 'POST', body: JSON.stringify(requestId), signal});
        if (!started.answer) {
          const context = messages.filter(m => m.role === 'assistant' || m.role === 'user').slice(-10)
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

  function choose(threadId?: string) {
    setHistoryOpen(false);
    if (threadId === id && !id) {setQuestion(''); setError(''); retry.current = null;}
    open(threadId);
  }

  const list = <><div className="support-list-heading"><strong>对话</strong>
    <Button size="small" icon={<PlusOutlined aria-hidden="true" />} onClick={() => choose()} disabled={busy}>新对话</Button></div>
    <div className="support-case-list">
      {!threads.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话" />}
      {threads.map(thread => <div className="conversation-row" key={thread.id}>
        <button className={`session-item ${id === thread.id ? 'selected' : ''}`} disabled={busy} onClick={() => choose(thread.id)}>{thread.title}</button>
        <ConversationMenu title={thread.title} path={conversations + '/' + thread.id} disabled={busy}
          renamed={name => setThreads(previous => previous.map(item => item.id === thread.id ? {...item, title: name} : item))}
          deleted={() => {setThreads(previous => previous.filter(item => item.id !== thread.id)); if (id === thread.id) choose();}} />
      </div>)}
    </div></>;

  function bubble(role: string, content: string, key: string | number) {
    return <article className={`support-message ${role}`} key={key}><div className="support-bubble"><div className="message-body">{content}</div></div></article>;
  }

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
        {!messages.length && !pending && !busy && <div className="plain-assistant-greeting"><h2>今天想聊点什么？</h2></div>}
        {messages.map((item, i) => bubble(item.role, item.content, i))}
        {pending && bubble('user', pending, 'pending')}
        {busy && bubble('assistant', draft || '正在回复…', 'reply')}
      </div>
      <div className="support-composer">
        {error && <div className="assistant-inline-error" role="alert">{error}<Button type="text" size="small" onClick={() => setError('')}>关闭</Button></div>}
        <div className="support-composer-box">
          <textarea aria-label="给维护助手发消息" placeholder="给维护助手发消息" value={question}
            rows={2} maxLength={8000} disabled={busy} onChange={e => setQuestion(e.target.value)} onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); void send();}
            }} />
          <div className="support-composer-tools">
            <Button size="small" icon={<ToolOutlined aria-hidden="true" />} onClick={diagnose} disabled={busy}>网站排查</Button>
            {busy && runId ? <Button size="small" onClick={() => void post('/api/agent/runs/' + runId + '/cancel').catch(e => setError(e.message))}>停止生成</Button>
              : <Button aria-label="发送" title="发送" type="primary" icon={<ArrowUpOutlined />} disabled={busy || !question.trim()} onClick={() => void send()} />}
          </div>
        </div>
      </div>
    </div>
  </div>;
}
