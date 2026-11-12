import type { AgentEvent, Auth, Workflow } from './types';

const storageKey = 'opspilot.auth';
export const formatTime = (value: string) => new Date(/[zZ]$|[+-]\d{2}:\d{2}$/.test(value) ? value : value + 'Z').toLocaleString();
export function savedAuth(): Auth | null {
  try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { return null; }
}
export function saveAuth(auth: Auth | null) {
  if (auth) sessionStorage.setItem(storageKey, JSON.stringify(auth)); else sessionStorage.removeItem(storageKey);
}
function headers(init?: HeadersInit) {
  const value = new Headers(init);
  const auth = savedAuth();
  if (auth) value.set('Authorization', `Bearer ${auth.access_token}`);
  return value;
}
async function checked(response: Response) {
  if (response.status === 401) window.dispatchEvent(new Event('opspilot:unauthorized'));
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === 'string' ? body.detail : `请求失败（${response.status}）`);
  }
  return response;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const requestHeaders = headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) requestHeaders.set('Content-Type', 'application/json');
  return (await checked(await fetch(path, { ...init, headers: requestHeaders }))).json();
}
export const post = <T>(path: string, body?: unknown) => api<T>(path, {method: 'POST', body: body === undefined ? undefined : JSON.stringify(body)});
export async function fileBlob(path: string) {
  return (await checked(await fetch(path, {headers: headers()}))).blob();
}
export async function downloadFile(path: string, name: string) {
  const url = URL.createObjectURL(await fileBlob(path));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export async function streamChat(sessionId: string, message: string, onEvent: (event: AgentEvent) => void, signal: AbortSignal, workflow: Workflow = 'auto') {
  const response = await checked(await fetch('/api/agent/chat/stream', {
    method: 'POST', headers: headers({'Content-Type': 'application/json'}),
    body: JSON.stringify({session_id: sessionId, message, workflow}), signal,
  }));
  if (!response.body) throw new Error('浏览器不支持流式响应');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', completed = false;
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer = (buffer + decoder.decode(value, {stream: !done})).replace(/\r\n/g, '\n');
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!data) continue;
        const event: AgentEvent = JSON.parse(data);
        if (event.type === 'done' || event.type === 'error') completed = true;
        onEvent(event);
      }
      if (done) break;
    }
    if (!completed) throw new Error('连接已中断，可重新打开会话查看已保存的结果');
  } finally { reader.releaseLock(); }
}
