import { useState } from 'react';
import { Button, Empty, Space, Tag } from 'antd';
import type { Site, SupportEntry } from './types';

export const siteEnvironments:Record<string,string>={unknown:'未分类',production:'生产',staging:'预发布',test:'测试',development:'开发'};

export default function SiteDependencies({site,ask}:{site:Site;ask?:(entry:SupportEntry)=>void}){
  const [selected,setSelected]=useState('');
  const links=site.dependencies||[];
  const upstream=[...new Set(links.filter(item=>item.source===selected).map(item=>item.target))];
  const downstream=[...new Set(links.filter(item=>item.target===selected).map(item=>item.source))];
  return <section aria-label="网站依赖关系图">
    {!links.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未登记组件依赖，可在编辑网站中补充"/>:<div className="dependency-map">{links.map((item,index)=><div className={`dependency-link ${selected&&(item.source===selected||item.target===selected)?'selected':''}`} key={index}><Button aria-pressed={selected===item.source} onClick={()=>setSelected(item.source)}>{item.source}</Button><span className="dependency-arrow" aria-label="依赖于">→</span><Button aria-pressed={selected===item.target} onClick={()=>setSelected(item.target)}>{item.target}</Button>{item.note&&<small>{item.note}</small>}</div>)}</div>}
    {selected&&<div className="dependency-selection"><Space wrap><strong>{selected}</strong><Button type="text" size="small" onClick={()=>setSelected('')}>取消选择</Button></Space><p>直接依赖：{upstream.length?upstream.map(name=><Tag key={name}>{name}</Tag>):'未登记'}</p><p>直接使用它的组件：{downstream.length?downstream.map(name=><Tag key={name}>{name}</Tag>):'未登记'}</p>{ask&&<Button onClick={()=>ask({siteId:site.id,workflow:'consult',question:`请结合这个网站已登记的依赖关系，说明「${selected}」异常时应先检查哪些上下游。我尚未提交新的现场证据。`})}>在旁边询问此组件</Button>}</div>}
  </section>;
}
