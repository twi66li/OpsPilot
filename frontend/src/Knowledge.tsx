import { useCallback, useEffect, useState } from 'react';
import { Alert, App, Button, Card, Empty, Input, Modal, Progress, Select, Space, Table, Tabs, Tag } from 'antd';
import { api, downloadFile, post } from './api';
import type { Hit, KnowledgeDoc, Version } from './types';

const statusText: Record<string, string> = {uploaded: '等待处理', parsing: '解析中', chunking: '切分中', indexing: '索引中', ready: '可检索', failed: '处理失败'};
export default function Knowledge({onSource, canWrite, personal = false}: {onSource: (hit: Hit) => void; canWrite: boolean; personal?: boolean}) {
  const {message} = App.useApp();
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]), [selected, setSelected] = useState<KnowledgeDoc | null>(null);
  const [versions, setVersions] = useState<Version[]>([]), [version, setVersion] = useState<Version | null>(null);
  const [versionId, setVersionId] = useState(''), [modal, setModal] = useState(false), [updating, setUpdating] = useState(false);
  const [file, setFile] = useState<File | null>(null), [parsed, setParsed] = useState<File | null>(null), [title, setTitle] = useState('');
  const [uploading, setUploading] = useState(false), [actionBusy, setActionBusy] = useState(false);
  const listPath = personal ? '/api/support/private-documents' : '/api/kb/documents';
  const refresh = useCallback(async () => setDocs(await api<KnowledgeDoc[]>(listPath)), [listPath]);
  useEffect(() => {refresh().catch(e => message.error(e.message));}, [refresh, message]);
  useEffect(() => {
    let active = true; setVersion(null); setVersionId(''); setVersions([]);
    if (selected) api<Version[]>(`/api/kb/documents/${selected.id}/versions`).then(items => {
      if (active) {setVersions(items); setVersionId(items[0]?.id || '');}
    }).catch(e => {if (active) message.error(e.message);});
    return () => {active = false;};
  }, [selected, message]);
  useEffect(() => {
    if (!versionId) return;
    let active = true; let timer: ReturnType<typeof setTimeout>;
    setVersion(null);
    async function poll() {
      try {
        const status = await api<Version>(`/api/kb/versions/${versionId}/status`);
        if (!active) return;
        setVersion(previous => ({...previous, ...status}));
        setVersions(previous => previous.map(item => item.id === status.id ? status : item));
        if (['ready', 'failed'].includes(status.status)) {
          const details = await api<Version>(`/api/kb/versions/${versionId}`);
          if (active) {setVersion(details); await refresh();}
        } else timer = setTimeout(poll, 1200);
      } catch (error) {if (active) message.error((error as Error).message);}
    }
    void poll(); return () => {active = false; clearTimeout(timer);};
  }, [versionId, refresh, message]);
  function openUpload(update: boolean) {setUpdating(update); setFile(null); setParsed(null); setTitle(''); setModal(true);}
  async function submit() {
    if (!file) {message.warning('请先选择原文件'); return;}
    const body = new FormData(); body.set('file', file); body.set('title', title);
    if (updating && selected) body.set('doc_id', selected.id);
    if (parsed) body.set('parsed_file', parsed);
    setUploading(true);
    try {
      const uploaded = await api<Version & {duplicate?: boolean}>(personal ? '/api/support/documents/upload' : '/api/kb/documents/upload', {method: 'POST', body});
      const items = await api<KnowledgeDoc[]>(listPath); setDocs(items);
      const doc = items.find(item => item.id === uploaded.doc_id)!;
      setSelected(doc); setModal(false);
      message.success(uploaded.duplicate ? '相同文件已有入库记录，已打开对应文档' : '已创建入库任务');
    } catch (error) {message.error((error as Error).message);} finally {setUploading(false);}
  }
  async function versionAction(action: 'retry' | 'activate') {
    if (!version) return;
    setActionBusy(true);
    try {
      await post(`/api/kb/versions/${version.id}/${action}`);
      if (selected) setSelected({...selected});
      message.success(action === 'retry' ? '已重新提交入库任务' : '有效版本已切换');
    } catch (error) {message.error((error as Error).message);} finally {setActionBusy(false);}
  }
  return <div className="page-stack">
    {personal && <Alert type="info" title="手册只属于你的个人空间。上传前请移除密码、令牌和无关个人信息；日志请在排障会话中脱敏后提交。" />}
    <div className="page-title"><div><h2>{personal ? '我的网站手册' : '运维知识库'}</h2><p>{personal ? '上传部署说明或维护手册，入库完成后在网站背景中关联。当前日志请在排障会话中提交。' : '管理手册、解析进度与有效版本，保留每次调查引用的来源。'}</p></div>
      <Space><Button onClick={() => refresh().catch(e => message.error(e.message))}>刷新</Button><Button type="primary" disabled={!canWrite} onClick={() => openUpload(false)}>上传文档</Button></Space>
    </div>
    <div className="knowledge-grid">
      <Card title={`文档 · ${docs.length}`} className="document-list">
        {docs.length ? docs.map(doc => <button className={`document-item ${selected?.id === doc.id ? 'selected' : ''}`} key={doc.id} onClick={() => setSelected(doc)}>
          <strong>{doc.title}</strong><span>{doc.source || '正文录入'}</span><small>{doc.chunk_count} 个有效片段</small>
        </button>) : <Empty description="上传第一份运维手册" />}
      </Card>
      <Card title={selected?.title || '解析与切分预览'} extra={selected && <Button disabled={!canWrite} onClick={() => openUpload(true)}>上传新版本</Button>}>
        {!selected ? <Empty description="选择左侧文档查看详情" /> : !versions.length ? <Alert type="info" title="此文档通过正文接口录入。上传新版本后可查看页码、切分与版本历史。" /> : <>
          <Space wrap className="version-bar"><span>版本</span><Select aria-label="知识版本" value={versionId} onChange={setVersionId} style={{minWidth: 230}}
            options={versions.map(v => ({value: v.id, label: `v${v.number} · ${statusText[v.status] || v.status}${v.active ? ' · 当前生效' : ''}`}))} />
            {version && <Tag color={version.status === 'failed' ? 'error' : version.status === 'ready' ? 'success' : 'processing'}>{statusText[version.status]}</Tag>}
          </Space>
          {version && <>
            <Progress percent={version.progress} status={version.status === 'failed' ? 'exception' : undefined} />
            <div className="muted">{version.filename} · {version.parser || '待解析'} · SHA-256 {version.content_hash.slice(0, 16)}…</div>
            {version.error && <Alert type="error" title={version.error} className="section-gap" action={<Button loading={actionBusy} disabled={!canWrite} onClick={() => versionAction('retry')}>重试</Button>} />}
            {version.warnings?.length > 0 && <Alert type="warning" title="解析提示" description={version.warnings.join('；')} className="section-gap" />}
            <Space className="section-gap"><Button onClick={() => downloadFile(`/api/kb/versions/${version.id}/file`, version.filename).catch(e => message.error(e.message))}>下载原文件</Button>
              {version.status === 'ready' && !version.active && <Button loading={actionBusy} disabled={!canWrite} onClick={() => versionAction('activate')}>设为有效版本</Button>}</Space>
            <Tabs items={[
              {key: 'pages', label: `解析正文 · ${version.pages?.length || 0} 页/段`, children: <div className="preview-scroll">{version.pages?.map((page, i) => <section key={i}><h4>{page.page ? `物理第 ${page.page} 页` : '文本正文'}</h4><pre className="source-text">{page.text}</pre></section>)}</div>},
              {key: 'chunks', label: `检索片段 · ${version.chunks?.length || 0}`, children: <Table<Hit> size="small" rowKey="chunk_id" dataSource={version.chunks} pagination={{pageSize: 8}} columns={[
                {title: '位置', render: (_, hit) => <><Tag>{hit.content_type}</Tag>{hit.page_start ? `第${hit.page_start}页` : '文本'}</>},
                {title: '片段', render: (_, hit) => <span className="snippet">{hit.text.slice(0, 110)}</span>},
                {title: '来源', render: (_, hit) => <Button type="link" onClick={() => onSource(hit)}>查看父块</Button>},
              ]} />},
            ]} />
          </>}
        </>}
      </Card>
    </div>
    <Modal title={updating ? '上传文档新版本' : '上传运维文档'} open={modal} onCancel={() => setModal(false)} onOk={submit} confirmLoading={uploading} okText="开始入库" destroyOnHidden>
      <div className="form-field"><label htmlFor="knowledge-title">文档标题（可选）</label><Input id="knowledge-title" value={title} onChange={e => setTitle(e.target.value)} maxLength={200} /></div>
      <div className="form-field"><label htmlFor="knowledge-file">原文件 · PDF / Markdown / TXT，最大10MB</label><input id="knowledge-file" type="file" accept=".pdf,.md,.txt" onChange={e => setFile(e.target.files?.[0] || null)} /></div>
      <div className="form-field"><label htmlFor="knowledge-parsed">MinerU content_list.json（复杂PDF可选）</label><input id="knowledge-parsed" type="file" accept=".json" onChange={e => setParsed(e.target.files?.[0] || null)} /></div>
      <Alert type="info" title="普通PDF直接提取文本。扫描件或复杂表格请先在本机解析，再同时上传原PDF和MinerU结果。" />
    </Modal>
  </div>;
}
