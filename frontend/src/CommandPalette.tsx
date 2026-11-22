import { useEffect, useRef, useState } from 'react';
import { Alert, Empty, Input, Modal, Spin, Tag } from 'antd';
import { api } from './api';
import type { Hit, KnowledgeDoc, PageData, PersonalCase, Site } from './types';
import InfoTip from './InfoTip';

type Item = {id:string; title:string; kind:string; open:()=>void};
type Article = {doc_id:string; title:string; content:string; url:string; applicable_version:string; checked_at:string};

export default function CommandPalette({open, close, navigate, onSource, guest, canApprove, admin, drillEnabled}: {
  open:boolean; close:()=>void; navigate:(url:string)=>void; onSource:(hit:Hit)=>void; guest:boolean; canApprove:boolean; admin:boolean; drillEnabled:boolean;
}) {
  const [query,setQuery]=useState(''),[items,setItems]=useState<Item[]>([]),[active,setActive]=useState(0),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const input=useRef<import('antd').InputRef>(null),list=useRef<HTMLDivElement>(null);
  useEffect(()=>{if(open){setQuery('');setActive(0);}},[open]);
  useEffect(()=>{
    if(!open)return;
    let current=true;
    setItems([]);setActive(0);setBusy(true);setError('');
    const timer=setTimeout(async()=>{
      const q=encodeURIComponent(query.trim());
      const results=await Promise.allSettled([
        api<PageData<Site>>(`/api/workspace/sites?page=1&page_size=6&q=${q}`),
        api<PageData<PersonalCase>>(`/api/workspace/cases?case_kind=problem&page=1&page_size=6&q=${q}`),
        api<PageData<KnowledgeDoc>>(`/api/workspace/documents?page=1&page_size=6&q=${q}`),
        api<Article[]>('/api/support/library'),
      ]);
      if(!current)return;
      const [sites,cases,docs,articles]=results;
      const go=(url:string)=>()=>navigate(url);
      const values:Item[]=[];
      if(sites.status==='fulfilled')values.push(...sites.value.items.map(item=>({id:'site:'+item.id,title:item.name,kind:'网站',open:go('/websites/'+encodeURIComponent(item.id))})));
      if(cases.status==='fulfilled')values.push(...cases.value.items.map(item=>({id:'case:'+item.id,title:item.title||'未命名问题',kind:'问题',open:go('/issues/'+encodeURIComponent(item.id))})));
      if(docs.status==='fulfilled')values.push(...docs.value.items.map(item=>({id:'doc:'+item.id,title:item.title,kind:'私人资料',open:go('/knowledge/'+encodeURIComponent(item.id)+'?tab=private')})));
      if(articles.status==='fulfilled')values.push(...articles.value.filter(item=>item.title.toLowerCase().includes(query.trim().toLowerCase())).slice(0,6).map(item=>({id:'official:'+item.doc_id,title:item.title,kind:'官方资料',open:()=>onSource({chunk_id:'',doc_id:item.doc_id,doc_title:item.title,text:item.content,parent_text:item.content,source:item.url,source_kind:'official',applicable_version:item.applicable_version,checked_at:item.checked_at})})));
      const actions=[['智能维护助手','/support'],['我的网站','/websites'],['维护资料库','/knowledge'],...(drillEnabled?[['故障演练','/investigation']]:[]),...(!guest?[['记录新问题','/issues?create=1'],['上传手册','/knowledge?tab=private&upload=1']]:[]),...(canApprove?[['审批与工单','/operations']]:[]),...(admin?[['评测记录','/evaluation']]:[])];
      values.push(...actions.filter(([title])=>title.includes(query.trim())).map(([title,url])=>({id:'action:'+url,title,kind:'操作',open:go(url)})));
      setItems(values);setBusy(false);setError(results.some(result=>result.status==='rejected')?'部分搜索结果加载失败，可修改关键词重试。':'');
    },200);
    return()=>{current=false;clearTimeout(timer);};
  },[open,query,navigate,onSource,guest,canApprove,admin,drillEnabled]);
  useEffect(()=>{
    const node=list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    if(node&&list.current){const top=node.offsetTop,bottom=top+node.offsetHeight;if(top<list.current.scrollTop)list.current.scrollTop=top;else if(bottom>list.current.scrollTop+list.current.clientHeight)list.current.scrollTop=bottom-list.current.clientHeight;}
  },[active]);
  function choose(item:Item){close();item.open();}
  return <Modal className="command-palette" open={open} onCancel={close} footer={null} title={<>搜索网站、问题与资料 <InfoTip label="搜索范围说明">每类最多显示六条匹配结果，资料按标题查找。完整结果可到相应列表继续查看。</InfoTip></>} afterOpenChange={shown=>{if(shown)input.current?.focus();}}>
    <Input ref={input} aria-label="全局搜索关键词" placeholder="输入名称、问题标题或常用操作" value={query} allowClear onChange={event=>setQuery(event.target.value)} onKeyDown={event=>{
      if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();setActive(value=>items.length?(value+(event.key==='ArrowDown'?1:-1)+items.length)%items.length:0);}
      if(event.key==='Enter'&&items[active]){event.preventDefault();choose(items[active]);}
    }} aria-controls="command-results" aria-activedescendant={items[active]?'command-result-'+active:undefined}/>
    {error&&<Alert type="warning" title={error}/>}
    <div className="command-results" ref={list} id="command-results" role="listbox" aria-label="搜索结果" aria-busy={busy}>
      {busy?<div className="command-loading"><Spin aria-label="正在搜索"/></div>:items.length?items.map((item,index)=><button key={item.id} id={'command-result-'+index} data-index={index} role="option" aria-selected={index===active} className={index===active?'active':''} onMouseEnter={()=>setActive(index)} onClick={()=>choose(item)}><span>{item.title}</span><Tag>{item.kind}</Tag></button>):<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有找到可访问的匹配记录"/>}
    </div>
  </Modal>;
}
