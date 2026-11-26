import InfoTip from './InfoTip';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { App, Button, Card, Input, Select, Space, Tabs, Tag } from 'antd';
import { api } from './api';
import type { Hit, SupportEntry } from './types';
import Knowledge from './Knowledge';
import ScenarioGuides from './ScenarioGuides';
import RetrievalLab from './RetrievalLab';

interface Article { doc_id: string; title: string; product: string; applicable_version: string; checked_at: string; url: string; content: string }
export default function OfficialLibrary({onSource,assist}: {onSource: (hit: Hit) => void;assist:(entry:SupportEntry)=>void}) {
  const {message} = App.useApp();
  const location=useLocation(),navigate=useNavigate(),[params,setParams]=useSearchParams(),docId=location.pathname.split('/')[2];
  const [articles, setArticles] = useState<Article[]>([]), [query, setQuery] = useState('');
  const [product, setProduct] = useState<string>();
  useEffect(() => {api<Article[]>('/api/support/library').then(setArticles).catch(e => message.error(e.message));}, [message]);
  return <div className="page-stack"><h2>维护资料库 <InfoTip label="资料库使用说明">内置资料可直接使用；私人手册用于补充网站环境差异，上传不是必需步骤。</InfoTip></h2>
    <Tabs activeKey={params.get('tab') || (docId?'private':'scenarios')} onChange={tab=>setParams({tab})} items={[{key:'scenarios',label:'按异常场景查找',children:<ScenarioGuides onSource={onSource} assist={assist} />},{key: 'official', label: '官方来源排障库', children: <>
      <div className="library-help info-inline-label"><span>整理自官方文档，请结合适用版本核对</span><InfoTip label="官方资料说明">指南依据官方资料整理，保留出处与核对日期；请核对适用版本，资料不是实时监控结果。</InfoTip></div>
      <Space className="section-gap section-bottom" wrap><Input.Search aria-label="搜索内置资料" placeholder="搜索连接池、502、Redis、K8s、JVM…" value={query} onChange={e => setQuery(e.target.value)} style={{width:300}} /><Select aria-label="按技术组件筛选资料" allowClear value={product} onChange={setProduct} placeholder="全部组件" style={{width:180}} options={[...new Set(articles.map(a=>a.product))].map(p=>({value:p,label:p}))} /></Space>
      <div className="home-cards">{articles.filter(a => (!product || a.product === product) && (a.title + a.product + a.content).toLowerCase().includes(query.toLowerCase())).map(a => <Card key={a.doc_id} title={a.title}>
        <Tag>{a.product}</Tag><p>{a.content.slice(0, 100)}…</p><p className="muted">{a.applicable_version}<br />核对日期：{a.checked_at}</p>
        <div className="card-actions"><Button onClick={() => onSource({chunk_id: '', doc_id: a.doc_id, doc_title: a.title, text: a.content, parent_text: a.content,
          source: a.url, source_kind: 'official', applicable_version: a.applicable_version, checked_at: a.checked_at})}>阅读指南与来源</Button><Button type="primary" onClick={()=>assist({workflow:"consult",question:`请解释「${a.title}」（适用版本：${a.applicable_version}）的关键检查方法和适用条件。我还未提交本次故障证据。`})}>询问这份指南</Button></div>
      </Card>)}</div>
    </>}, {key: 'private', label: '我的网站手册（可选）', children: <Knowledge onSource={onSource} canWrite personal initialId={docId} opened={id=>navigate('/knowledge/'+id+'?tab=private')}/>}, {key:'retrieval',label:'测试资料检索',children:<RetrievalLab onSource={onSource}/>}]} />
  </div>;
}
