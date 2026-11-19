import { useCallback, useEffect, useState } from 'react';
import { App, Badge, Button, Empty, Popover, Tag } from 'antd';
import { BellOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { api } from './api';
import type { MaintenanceTodo } from './types';
import TodoAction, { isActiveTodo } from './TodoAction';

export default function TodoCenter({canWrite=false}:{canWrite?:boolean}){
  const {message}=App.useApp(),navigate=useNavigate(),[open,setOpen]=useState(false),[items,setItems]=useState<MaintenanceTodo[]>([]);
  const refresh=useCallback(()=>api<{items:MaintenanceTodo[]}>('/api/maintenance/todos').then(r=>setItems(r.items)),[]);
  useEffect(()=>{
    const load=()=>void refresh().catch(()=>{});load();
    const timer=setInterval(load,60000);
    window.addEventListener('opspilot:workspace-changed',load);window.addEventListener('focus',load);
    return()=>{clearInterval(timer);window.removeEventListener('opspilot:workspace-changed',load);window.removeEventListener('focus',load);};
  },[refresh]);
  const active=items.filter(isActiveTodo).filter(item=>item.kind!=='approval'||canWrite);
  return <Popover open={open} onOpenChange={value=>{setOpen(value);if(value)void refresh().catch(e=>message.error(e.message));}} trigger="click" title="待办与近期任务" content={<div className="todo-popover">
    {!active.length&&<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无待办"/>}
    {active.map(item=><div key={item.id} className="todo-entry"><Tag>{item.state}</Tag><strong>{item.title}</strong><TodoAction item={item} canWrite={canWrite} navigate={url=>{setOpen(false);navigate(url);}} changed={()=>{setItems(previous=>previous.filter(row=>row.id!==item.id));void refresh();}}/></div>)}

  </div>}><Button className="todo-trigger" type="text" aria-label={`待办 ${active.length}`} title={`待办 ${active.length}`}><Badge count={active.length} showZero overflowCount={99} size="small" color={active.length?undefined:'var(--text-3)'}><BellOutlined aria-hidden="true"/></Badge></Button></Popover>;
}
