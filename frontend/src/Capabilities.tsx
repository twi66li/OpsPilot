import InfoTip from './InfoTip';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Drawer, Empty, Space, Tag } from 'antd';
import { api, formatTime } from './api';
import StatusTag from './StatusTag';

type Data={items:{key:string;title:string;state:string;description:string}[];checks:Record<string,string>;recent:{id:string;question:string;status:string;reason:string;updated_at:string;url:string}[]};
const states:Record<string,string>={available:'检查通过',configured:'已配置，未请求验证',offline:'离线模式',manual:'手动提交',read_only:'访客只读',unavailable:'当前不可用'};
const names:Record<string,string>={database:'业务记录',vector_store:'资料索引',cache:'缓存',ingestion_worker:'资料入库',dependencies:'服务依赖'};
export default function Capabilities({open,close,navigate}:{open:boolean;close:()=>void;navigate:(url:string)=>void}){
  const [data,setData]=useState<Data|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const load=useCallback(async()=>{setBusy(true);setError('');try{setData(await api<Data>('/api/maintenance/capabilities'));}catch(e){setError((e as Error).message);}finally{setBusy(false);}},[]);
  useEffect(()=>{if(open)void load();},[open,load]);
  return <Drawer open={open} onClose={close} title="当前能力与连接状态" width={600} extra={<Button loading={busy} onClick={()=>void load()}>刷新状态</Button>}><div className="page-stack capability-panel">
    {error&&<Alert type="error" title="能力状态加载失败" description={error}/>}
    {data&&<><div className="info-inline-label"><span>基础依赖检查（仅反映本次快照）</span><InfoTip label="能力检查范围说明">连接检查只反映本次结果。查看本面板不会请求聊天模型，也不会检查你的真实网站。</InfoTip></div><div className="capability-checks">{Object.entries(data.checks).map(([name,state])=><Tag key={name} color={state==='unavailable'?'error':undefined}>{names[name]||name} · {state==='ok'?'通过':state==='memory'?'进程内模式':'不可用'}</Tag>)}</div>
      {data.items.map(item=><Card key={item.key} size="small" title={item.title} extra={<Tag color={item.state==='unavailable'?'error':undefined}>{states[item.state]||item.state}</Tag>}><p>{item.description}</p></Card>)}
      <h3>最近的分析记录</h3>{data.recent.length?data.recent.map(item=><section className="capability-run" key={item.id}><Space wrap><StatusTag status={item.status}/><small>{formatTime(item.updated_at)}</small></Space><strong>{item.question}</strong><p>{item.reason}</p><Button onClick={()=>{close();navigate(item.url);}}>查看这次分析</Button></section>):<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有属于当前账号的分析记录"/>}
    </>}
  </div></Drawer>;
}
