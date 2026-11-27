import { Alert, Button, Form, Input, Modal, Space } from 'antd';
import type { PersonalModel } from './api';

export type { PersonalModel } from './api';

const DEFAULT_PERSONAL_MODEL: PersonalModel = {endpoint: 'https://api.deepseek.com', model: 'deepseek-flash'};
const LOOPBACK_HOSTS = ['localhost', '127.0.0.1', '[::1]'];

function modelUrl(endpoint: string) {
  const url = new URL(endpoint);
  const loopback = LOOPBACK_HOSTS.includes(url.hostname);
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) ||
      url.origin === window.location.origin || url.username || url.password || url.search || url.hash) {
    throw new Error('请输入独立的 HTTPS 模型网关，或本机回环地址');
  }
  return url.href.replace(/\/+$/, '');
}

export default function AssistantSettings({value, save, close}: {
  value: PersonalModel | null; save: (model: PersonalModel | null) => void; close: () => void;
}) {
  const [form] = Form.useForm<PersonalModel>();
  return <Modal open title="模型设置" onCancel={close} footer={null}>
    <Form form={form} layout="vertical" initialValues={value ?? DEFAULT_PERSONAL_MODEL}
      onFinish={fields => {
        try {
          const endpoint = modelUrl(fields.endpoint.trim());
          save({endpoint, model: fields.model.trim(), key: fields.key?.trim()});
        } catch (error) { form.setFields([{name: 'endpoint', errors: [(error as Error).message]}]); }
      }}>
      {!window.isSecureContext && (
        <Alert
          type="warning"
          showIcon
          className="section-bottom"
          title="当前连接未使用 HTTPS"
          description="发起对话时 API Key 会经网络发往后端临时调用，存在明文传输风险。建议仅使用临时或低额度 Key，正式环境请通过 HTTPS 访问。"
        />
      )}
      <p className="muted">切换后将统一用于整套助手工作流（聊天问答、手册检索、证据诊断与复测），检索、权限与审批仍由 OpsPilot 管理。凭据仅保存在当前页面内存，发给后端临时调用，不落库、不记录日志，刷新页面即清除。</p>
      <Form.Item name="endpoint" label="Base URL" rules={[{required: true, whitespace: true}]}>
        <Input placeholder="https://你的网关/v1" />
      </Form.Item>
      <Form.Item name="model" label="模型 ID" rules={[{required: true, whitespace: true}]}><Input /></Form.Item>
      <Form.Item name="key" label="API Key" dependencies={['endpoint']} rules={[({getFieldValue}) => {
        const endpoint = (getFieldValue('endpoint') || '').trim();
        const loopback = URL.canParse(endpoint) && LOOPBACK_HOSTS.includes(new URL(endpoint).hostname);
        return loopback ? {} : {required: true, whitespace: true, message: '请输入 API Key'};
      }]}>
        <Input.Password autoComplete="off" />
      </Form.Item>
      <Space>
        <Button type="primary" htmlType="submit">使用自带模型</Button>
        <Button onClick={() => save(null)}>使用平台模型</Button>
      </Space>
    </Form>
  </Modal>;
}
