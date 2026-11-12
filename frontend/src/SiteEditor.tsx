import { useEffect, useState } from 'react';
import { App, Form, Input, Modal, Select } from 'antd';
import { api } from './api';
import type { KnowledgeDoc, Site } from './types';

export default function SiteEditor({open, site, close, saved}: {open: boolean; site?: Site; close: () => void; saved: (site: Site) => void}) {
  const {message} = App.useApp(), [form] = Form.useForm();
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]), [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    form.resetFields(); form.setFieldsValue(site || {name: '我的网站', doc_ids: []});
    api<KnowledgeDoc[]>('/api/support/private-documents').then(setDocs).catch(e => message.error(e.message));
  }, [open, site, form, message]);
  async function submit() {
    try {
      const values = await form.validateFields(); setBusy(true);
      const result = await api<Site>(site ? `/api/support/sites/${site.id}` : '/api/support/sites', {method: site ? 'PUT' : 'POST', body: JSON.stringify(values)});
      saved(result); message.success('网站信息已保存；已有问题仍保留当时的背景快照。');
    } catch (error) {if (error instanceof Error) message.error(error.message);} finally {setBusy(false);}
  }
  return <Modal open={open} title={site ? '编辑网站' : '添加网站'} onCancel={close} onOk={submit} confirmLoading={busy} okText="保存网站">
    <p>只填写已知环境，不需要服务器账号或监控接入。手册先在资料库上传，完成入库后可在这里关联。</p>
    <Form form={form} layout="vertical">
      <Form.Item name="name" label="网站名称" rules={[{required: true}]}><Input maxLength={80} /></Form.Item>
      <Form.Item name="framework" label="框架"><Input maxLength={120} placeholder="FastAPI / Django；不知道可留空" /></Form.Item>
      <Form.Item name="deployment" label="部署方式"><Input maxLength={120} placeholder="Docker Compose / Linux直接部署" /></Form.Item>
      <Form.Item name="database" label="数据库"><Input maxLength={120} placeholder="PostgreSQL / MySQL" /></Form.Item>
      <Form.Item name="versions" label="软件版本"><Input maxLength={200} placeholder="Python 3.12、PostgreSQL 16" /></Form.Item>
      <Form.Item name="notes" label="维护说明 / 近期变更"><Input.TextArea maxLength={1200} rows={3} /></Form.Item>
      <Form.Item name="doc_ids" label="关联手册（可选）"><Select mode="multiple" options={docs.filter(d => d.chunk_count > 0).map(d => ({value: d.id, label: d.title}))} placeholder="只选择适用于这个网站的资料" /></Form.Item>
    </Form>
  </Modal>;
}
