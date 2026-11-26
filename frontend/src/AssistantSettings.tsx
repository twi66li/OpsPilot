import { Button, Form, Input, Modal, Space } from 'antd';
import type { ChatMessage } from './types';

export type PersonalModel = {endpoint: string; model: string; key?: string};

function modelUrl(endpoint: string) {
  const url = new URL(endpoint);
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) ||
      url.origin === window.location.origin || url.username || url.password || url.search || url.hash) {
    throw new Error('请输入独立的 HTTPS 模型网关，或本机回环地址');
  }
  return url.href.replace(/\/+$/, '') + '/chat/completions';
}

export async function askPersonalModel(config: PersonalModel, messages: Pick<ChatMessage, 'role' | 'content'>[], signal: AbortSignal) {
  const headers = new Headers({'Content-Type': 'application/json'});
  if (config.key) headers.set('Authorization', 'Bearer ' + config.key);
  const url = modelUrl(config.endpoint);
  let reply: {choices?: {message?: {content?: string}}[]};
  try {
    const response = await fetch(url, {
      method: 'POST', headers, signal: AbortSignal.any([signal, AbortSignal.timeout(60000)]),
      credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store',
      body: JSON.stringify({model: config.model.trim(), messages, stream: false, max_tokens: 2048}),
    });
    if (!response.ok) throw new Error();
    reply = await response.json();
  } catch {
    throw new Error('连接自带模型失败，请检查地址、模型、凭据和网关的跨域配置。');
  }
  const answer = reply.choices?.[0]?.message?.content;
  if (typeof answer !== 'string' || !answer.trim()) throw new Error('自带模型未返回可显示的回复');
  return (config.key ? answer.replaceAll(config.key, '[已隐藏凭据]') : answer).slice(0, 40000);
}

export default function AssistantSettings({value, save, close}: {
  value: PersonalModel | null; save: (model: PersonalModel | null) => void; close: () => void;
}) {
  const [form] = Form.useForm<PersonalModel>();
  return <Modal open title="模型设置" onCancel={close} footer={null}>
    {window.isSecureContext ? <Form form={form} layout="vertical" initialValues={value || undefined}
      onFinish={fields => {
        try {
          modelUrl(fields.endpoint.trim());
          save({endpoint: fields.endpoint.trim(), model: fields.model.trim(), key: fields.key?.trim()});
        } catch (error) { form.setFields([{name: 'endpoint', errors: [(error as Error).message]}]); }
      }}>
      <p className="muted">浏览器直接连接你的网关。凭据仅在当前页面内存中使用，OpsPilot 不接收、不保存。普通聊天的消息会发送给所选模型。</p>
      <Form.Item name="endpoint" label="模型 API 基础地址" rules={[{required: true, whitespace: true}]}>
        <Input placeholder="https://你的网关/v1" />
      </Form.Item>
      <Form.Item name="model" label="模型名称" rules={[{required: true, whitespace: true}]}><Input /></Form.Item>
      <Form.Item name="key" label="网关凭据或短期 API Key（选填）"><Input.Password autoComplete="off" /></Form.Item>
      <Space>
        <Button type="primary" htmlType="submit">使用自带模型</Button>
        <Button onClick={() => save(null)}>使用平台模型</Button>
      </Space>
    </Form> : <p>通过 HTTPS 打开 OpsPilot 后可以配置自带模型。</p>}
  </Modal>;
}
