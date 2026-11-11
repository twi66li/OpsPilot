import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Input, Tabs, Tag } from 'antd';
import { api } from './api';
import type { Hit } from './types';
import Knowledge from './Knowledge';

interface Article { doc_id: string; title: string; product: string; applicable_version: string; checked_at: string; url: string; content: string }
export default function OfficialLibrary({onSource}: {onSource: (hit: Hit) => void}) {
  const {message} = App.useApp();
  const [articles, setArticles] = useState<Article[]>([]), [query, setQuery] = useState('');
  useEffect(() => {api<Article[]>('/api/support/library').then(setArticles).catch(e => message.error(e.message));}, [message]);
  return <div className="page-stack"><div><h2>排障资料库</h2><p>内置资料可以直接使用；你的网站手册用于补充自己的环境差异，上传不是必需步骤。</p></div>
    <Tabs items={[{key: 'official', label: '官方来源排障库', children: <>
      <Alert type="info" title="以下是平台依据官方资料整理的中文排障指南，保留出处与核对日期；请核对适用版本，不代表实时监控结果。" />
      <Input.Search className="section-gap section-bottom" aria-label="搜索内置资料" placeholder="搜索连接池、502、Docker、Python…" value={query} onChange={e => setQuery(e.target.value)} />
      <div className="home-cards">{articles.filter(a => (a.title + a.product + a.content).toLowerCase().includes(query.toLowerCase())).map(a => <Card key={a.doc_id} title={a.title}>
        <Tag>{a.product}</Tag><p>{a.content.slice(0, 100)}…</p><p className="muted">{a.applicable_version}<br />核对日期：{a.checked_at}</p>
        <Button onClick={() => onSource({chunk_id: '', doc_id: a.doc_id, doc_title: a.title, text: a.content, parent_text: a.content,
          source: a.url, source_kind: 'official', applicable_version: a.applicable_version, checked_at: a.checked_at})}>阅读指南与来源</Button>
      </Card>)}</div>
    </>}, {key: 'private', label: '我的网站手册（可选）', children: <Knowledge onSource={onSource} canWrite personal />}]} />
  </div>;
}
