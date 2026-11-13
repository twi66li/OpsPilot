import { useCallback, useEffect, useState } from 'react';
import { App, Button, Empty, Popover, Space, Tag } from 'antd';
import { useNavigate } from 'react-router-dom';
import { api } from './api';
export default function TodoCenter(){
 const {message}=App.useApp(),navigate=useNavigate(),[open,setOpen]=useState(false),[items,setItems]=useState<{id:string;title:string;state:string;url:string}[]>([]);
 const refresh=useCallback(()=>api<{items:typeof items}>('/api/maintenance/todos').then(r=>setItems(r.items)),[]);
 useEffect(()=>{void refresh().catch(()=>{});const timer=setInterval(()=>void refresh().catch(()=>{}),60000);return()=>clearInterval(timer);},[refresh]);
 return <Popover open={open} onOpenChange={value=>{setOpen(value);if(value)void refresh().catch(e=>message.error(e.message));}} trigger="click" title="待办与近期任务" content={<div style={{maxWidth:300,maxHeight:420,overflow:'auto'}}>{!items.length&&<Empty description="暂无待办"/>}{items.map(item=><div key={item.id} className="section-bottom"><Space wrap><Tag>{item.state}</Tag><span>{item.title}</span><Button size="small" onClick={()=>{setOpen(false);navigate(item.url);}}>打开</Button></Space></div>)}</div>}><Button className="todo-trigger" size="small">待办 {items.length}</Button></Popover>;
}
