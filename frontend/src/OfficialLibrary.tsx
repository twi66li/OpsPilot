import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Input, Select, Space, Tabs, Tag } from 'antd';
import { api } from './api';
import type { Hit } from './types';
import Knowledge from './Knowledge';
import ScenarioGuides from './ScenarioGuides';
import RetrievalLab from './RetrievalLab';

interface Article { doc_id: string; title: string; product: string; applicable_version: string; checked_at: string; url: string; content: string }
export default function OfficialLibrary({onSource}: {onSource: (hit: Hit) => void}) {
  const {message} = App.useApp();
  const location=useLocation(),navigate=useNavigate(),[params,setParams]=useSearchParams(),docId=location.pathname.split('/')[2];
  const [articles, setArticles] = useState<Article[]>([]), [query, setQuery] = useState('');
  const [product, setProduct] = useState<string>();
  useEffect(() => {api<Article[]>('/api/support/library').then(setArticles).catch(e => message.error(e.message));}, [message]);
  return <div className="page-stack"><div><h2>排障资料库</h2><p>内置资料可以直接使用；你的网站手册用于补充自己的环境差异，上传不是必需步骤。</p></div>
    <Tabs activeKey={params.get('tab') || (docId?'private':'scenarios')} onChange={tab=>setParams({tab})} items={[{key:'retrieval',label:'测试资料检索',children:<RetrievalLab onSource={onSource}/>},{key:'scenarios',label:'按异常场景查找',children:<ScenarioGuides onSource={onSource} />},{key: 'official', label: '官方来源排障库', children: <>
      <Alert type="info" title="以下是平台依据官方资料整理的中文排障指南，保留出处与核对日期；请核对适用版本，不代表实时监控结果。" />
      <Space className="section-gap section-bottom" wrap><Input.Search aria-label="搜索内置资料" placeholder="搜索连接池、502、Docker、Python…" value={query} onChange={e => setQuery(e.target.value)} style={{width:300}} /><Select aria-label="按技术组件筛选资料" allowClear value={product} onChange={setProduct} placeholder="全部组件" style={{width:180}} options={[...new Set(articles.map(a=>a.product))].map(p=>({value:p,label:p}))} /></Space>
      <div className="home-cards">{articles.filter(a => (!product || a.product === product) && (a.title + a.product + a.content).toLowerCase().includes(query.toLowerCase())).map(a => <Card key={a.doc_id} title={a.title}>
        <Tag>{a.product}</Tag><p>{a.content.slice(0, 100)}…</p><p className="muted">{a.applicable_version}<br />核对日期：{a.checked_at}</p>
        <Button onClick={() => onSource({chunk_id: '', doc_id: a.doc_id, doc_title: a.title, text: a.content, parent_text: a.content,
          source: a.url, source_kind: 'official', applicable_version: a.applicable_version, checked_at: a.checked_at})}>阅读指南与来源</Button>
      </Card>)}</div>
    </>}, {key: 'private', label: '我的网站手册（可选）', children: <Knowledge onSource={onSource} canWrite personal initialId={docId} opened={id=>navigate('/knowledge/'+id+'?tab=private')}/>}]} />
  </div>;
}
