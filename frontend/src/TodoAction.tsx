import { useState } from 'react';
import { App, Button } from 'antd';
import { post, savedAuth } from './api';
import type { MaintenanceTodo } from './types';

export const isActiveTodo = (item:MaintenanceTodo) => item.kind !== 'completed' && item.state !== '最近已完成';
const labels:Record<string,string> = {continue_case:'继续处理',submit_result:'提交检查结果',retry_document:'重试入库',resume_analysis:'重新分析',view_run:'查看记录',review_approval:'前往审批'};

export default function TodoAction({item,navigate,changed,canWrite=false,primary=false}:{
  item:MaintenanceTodo;navigate:(url:string)=>void;changed:()=>void;canWrite?:boolean;primary?:boolean;
}) {
  const {message}=App.useApp(),[busy,setBusy]=useState(false);
  if(item.kind==='approval' && !canWrite)return null;
  async function act(){
    if(item.action!=='retry_document'){navigate(item.url);return;}
    if(!item.resource_id)return;
    setBusy(true);
    try{
      const result=await post<{items:{id:string;status:string}[]}>('/api/maintenance/documents/retry-failed',{ids:[item.resource_id]});
      const state=result.items.find(row=>row.id===item.resource_id)?.status;
      if(state==='blocked')throw new Error('这份资料暂不能重试，请打开资料查看具体原因。');
      message.success(state==='queued'?'已重新加入入库队列':'资料状态已更新');changed();
    }catch(error){message.error((error as Error).message);}finally{setBusy(false);}
  }
  return <Button type={primary?'primary':'default'} disabled={!!savedAuth()?.is_guest&&item.action==='retry_document'} loading={busy} onClick={()=>void act()}>{savedAuth()?.is_guest?'查看记录':labels[item.action||'']||'打开'}</Button>;
}
