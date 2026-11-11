import { useEffect, useState } from 'react';
import { Alert, App, Button, Descriptions, Drawer, Spin } from 'antd';
import { api, downloadFile } from './api';
import type { Hit } from './types';

export default function SourceDrawer({hit, close}: {hit: Hit | null; close: () => void}) {
  const [source, setSource] = useState<Hit | null>(null);
  const [loading, setLoading] = useState(false);
  const {message} = App.useApp();
  useEffect(() => {
    let active = true; setSource(null);
    if (!hit) return;
    setLoading(true);
    const request = hit.source_kind === 'official' ? Promise.resolve(hit) : hit.version_id ? api<Hit>(`/api/kb/chunks/${encodeURIComponent(hit.chunk_id)}`)
      : api<{content: string}>(`/api/kb/documents/${hit.doc_id}`).then(doc => ({...hit, parent_text: doc.content}));
    request.then(value => {if (active) setSource(value);}).catch(error => {if (active) message.error(error.message);})
      .finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [hit, message]);
  return <Drawer open={!!hit} onClose={close} title="引用来源" size="large">
    {loading ? <Spin /> : source && <>
      <Descriptions column={1} items={[
        {key: 'title', label: '文档', children: source.doc_title},
        {key: 'kind', label: '资料类型', children: source.source_kind === 'official' ? '平台整理 · 官方资料来源' : '网站手册'},
        ...(source.source_kind === 'official' ? [
          {key: 'applicable', label: '适用版本', children: source.applicable_version},
          {key: 'checked', label: '核对日期', children: source.checked_at},
          {key: 'url', label: '官方出处', children: <a href={source.source} target="_blank" rel="noreferrer">打开官方文档</a>},
        ] : []),
        {key: 'version', label: '版本', children: source.version ? `v${source.version}` : '正文文档'},
        {key: 'page', label: '物理页序', children: source.page_start ? `第 ${source.page_start} 页` : '文本文件无页码'},
        {key: 'heading', label: '章节', children: source.heading_path?.join(' / ') || '正文'},
      ]} />
      {source.file_url && <Button onClick={() => downloadFile(source.file_url!, source.source_name || source.source || '原文件.pdf').catch(e => message.error(e.message))}>下载原文件</Button>}
      <h3>检索命中</h3><pre className="source-text">{source.matched_text || source.text}</pre>
      <h3>所在章节</h3><pre className="source-text">{source.parent_text || source.text}</pre>
      <Alert type="info" title="引用保留当次使用的知识版本；文档更新后仍可回查历史依据。" />
    </>}
  </Drawer>;
}
