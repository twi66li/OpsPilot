import InfoTip from './InfoTip';
import { useEffect, useState } from 'react';
import { App, Button, Form, Input, Modal, Select, Space } from 'antd';
import { api } from './api';
import type { KnowledgeDoc, Site } from './types';
import { siteEnvironments } from './SiteDependencies';

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
  return <Modal open={open} title={site ? '编辑网站' : '添加网站'} onCancel={close} onOk={submit} confirmLoading={busy} okText="保存网站" width={640}>
    <InfoTip label="网站背景填写说明">只填写已知环境，不需要服务器账号或监控接入。手册先在资料库上传，完成入库后可在这里关联。</InfoTip>
    <Form form={form} layout="vertical">
      <Form.Item name="name" label="网站名称" rules={[{required: true}]}><Input maxLength={80} /></Form.Item>
      <div className="site-editor-metadata"><Form.Item name="environment" label="运行环境" initialValue="unknown"><Select options={Object.entries(siteEnvironments).map(([value,label])=>({value,label}))}/></Form.Item><Form.Item name="group" label="业务分组"><Input maxLength={80} placeholder="例如：订单业务"/></Form.Item><Form.Item name="owner" label="负责人"><Input maxLength={80}/></Form.Item></div>
      <Form.Item name="url" label="公开网址（可选，用于主动HTTP检查）" rules={[{type:'url',message:'请输入完整HTTP(S)网址'}]}><Input maxLength={1024} placeholder="https://example.com" /></Form.Item>
      <Form.Item name="framework" label="框架"><Input maxLength={120} placeholder="FastAPI / Django；不知道可留空" /></Form.Item>
      <Form.Item name="deployment" label="部署方式"><Input maxLength={120} placeholder="Docker Compose / Linux直接部署" /></Form.Item>
      <Form.Item name="database" label="数据库"><Input maxLength={120} placeholder="PostgreSQL / MySQL" /></Form.Item>
      <Form.Item name="versions" label="软件版本"><Input maxLength={200} placeholder="Python 3.12、PostgreSQL 16" /></Form.Item>
      <Form.Item name="notes" label="维护说明 / 近期变更"><Input.TextArea maxLength={1200} rows={3} /></Form.Item>
      <Form.List name="dependencies">{(fields,{add,remove})=><section className="dependency-editor"><strong>组件依赖关系（可选）</strong><InfoTip label="登记依赖关系说明">箭头从使用方指向它依赖的组件，例如 Nginx → 应用、应用 → PostgreSQL。仅登记关系，不自动连接组件。</InfoTip>{fields.map(field=><div className="dependency-editor-row" key={field.key}><Form.Item name={[field.name,'source']} rules={[{required:true,message:'填写使用方'}]}><Input aria-label={`依赖${field.name+1}使用方`} maxLength={80} placeholder="使用方"/></Form.Item><span aria-hidden="true">→</span><Form.Item name={[field.name,'target']} rules={[{required:true,message:'填写被依赖组件'}]}><Input aria-label={`依赖${field.name+1}被依赖方`} maxLength={80} placeholder="被依赖组件"/></Form.Item><Button danger type="text" aria-label={`移除依赖${field.name+1}`} onClick={()=>remove(field.name)}>移除</Button><Form.Item className="dependency-note" name={[field.name,'note']}><Input aria-label={`依赖${field.name+1}说明`} maxLength={200} placeholder="用途或注意事项（选填）"/></Form.Item></div>)}<Space><Button disabled={fields.length>=12} onClick={()=>add({source:'',target:'',note:''})}>添加依赖关系</Button><small className="muted">最多 12 条</small></Space></section>}</Form.List>
      <Form.Item name="doc_ids" label="关联手册（可选）"><Select mode="multiple" options={docs.filter(d => d.chunk_count > 0).map(d => ({value: d.id, label: d.title}))} placeholder="只选择适用于这个网站的资料" /></Form.Item>
    </Form>
  </Modal>;
}
