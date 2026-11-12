import { useCallback, useEffect, useState } from 'react';
import { App, Button, Card, Collapse, Input, Select, Space, Timeline } from 'antd';
import { api, downloadFile, formatTime, post } from './api';
import { caseStates } from './supportTypes';

type Event={id:string|number;kind:string;at:string;detail:Record<string,unknown>};
const labels:Record<string,string>={case_created:'登记问题',site_saved:'更新网站资料',problem_updated:'补充问题信息',status_changed:'更新处理状态',evidence_submitted:'提交证据',plan_confirmed:'确认检查方案',step_updated:'记录检查结果',case_archived:'归档记录',analysis_completed:'完成分析',metadata_changed:'更新严重度与标签',comment:'补充维护记录',http_probe:'主动检查HTTP响应'};
function description(event:Event){const d=event.detail;return [d.label,d.text,d.note,d.solution,d.status?caseStates[String(d.status)]||d.status:'',d.step?`第${d.step}步`:'',d.revision?`方案版本${d.revision}`:'',d.actor?`记录人：${d.actor}`:''].filter(Boolean).join(' · ');}
export default function MaintenanceTimeline({caseId,siteId,revision,severity='P2',labels:initialLabels=[]}:{caseId?:string;siteId?:string;revision?:string|number;severity?:string;labels?:string[]}) {
  const {message}=App.useApp(),[events,setEvents]=useState<Event[]>([]),[note,setNote]=useState(''),[level,setLevel]=useState(severity),[labels,setLabels]=useState(initialLabels),[busy,setBusy]=useState(false);
  const load=useCallback(async()=>{const r=await api<{events:Event[]}>(`/api/maintenance/${caseId?'cases':'sites'}/${caseId||siteId}/timeline`);setEvents(r.events);},[caseId,siteId]);
  useEffect(()=>{void load().catch(e=>message.error(e.message));},[load,revision,message]);
  useEffect(()=>{setLevel(severity);setLabels(initialLabels);},[caseId,severity,initialLabels.join(',')]);
  async function save(comment:boolean){if(!caseId)return;setBusy(true);try{if(comment){await post(`/api/maintenance/cases/${caseId}/comments`,{text:note});setNote('');}else{await api(`/api/maintenance/cases/${caseId}/metadata`,{method:'PUT',body:JSON.stringify({severity:level,labels})});}await load();message.success('已保存维护记录');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}
  return <Card title="维护时间线与交接" extra={caseId&&<Button onClick={()=>downloadFile(`/api/maintenance/cases/${caseId}/report`,`opspilot-${caseId}.md`).catch(e=>message.error(e.message))}>导出Markdown报告</Button>}>
    {caseId&&<Collapse className="section-bottom" items={[{key:'metadata',label:'严重度、标签与补充记录',children:<div className="page-stack"><Space wrap><Select aria-label="问题严重度" value={level} onChange={setLevel} options={['P0','P1','P2','P3'].map(value=>({value,label:value}))}/><Select aria-label="问题标签" mode="tags" maxCount={8} value={labels} onChange={setLabels} placeholder="添加标签" style={{minWidth:180}}/><Button loading={busy} onClick={()=>save(false)}>保存分类</Button></Space><Input.TextArea aria-label="维护补充说明" value={note} maxLength={2000} rows={3} onChange={e=>setNote(e.target.value)} placeholder="补充操作、结果或交接说明；提交前请检查敏感信息"/><Button loading={busy} disabled={!note.trim()} onClick={()=>save(true)}>保存补充说明</Button></div>}]} />}
    <Timeline items={events.map(e=>({key:e.id,content:<><strong>{labelsFor(e.kind)}</strong><small> · {formatTime(e.at)}</small><p>{description(e)}</p></>}))}/>{!events.length&&<p className="muted">新增证据、确认方案和处理结果后，这里会逐步形成维护记录。</p>}
  </Card>;
}
function labelsFor(kind:string){return labels[kind]||'维护活动';}
