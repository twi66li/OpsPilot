import { useState } from 'react';
import { Alert, App, Button, Card, Input, Modal, Select, Space } from 'antd';
import { copyText, post } from './api';

type Guide={label:string;command:string;purpose:string;scenario_notes?:string;risk?:string};
export default function CollectionGuide({useResult}:{useResult:()=>void}) {
  const {message}=App.useApp(),[open,setOpen]=useState(false),[scenario,setScenario]=useState('nginx'),[deployment,setDeployment]=useState('unknown'),[target,setTarget]=useState(''),[steps,setSteps]=useState<Guide[]>([]),[busy,setBusy]=useState(false);
  async function generate(){setBusy(true);try{const r=await post<{steps:Guide[]}>('/api/maintenance/collection-guide',{scenario,deployment,target});setSteps(r.steps);}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}
  return <><Button onClick={()=>setOpen(true)}>不知道如何取日志？</Button><Modal open={open} title="只读证据收集向导" onCancel={()=>setOpen(false)} footer={<Button onClick={()=>{setOpen(false);useResult();}}>我已收集结果，开始脱敏预览</Button>}>
    <Alert type="info" title="在你自己的服务器上执行，平台提供指引并接收你确认提交的结果。" />
    <Space wrap className="section-gap"><Select aria-label="异常场景" style={{width:160}} popupMatchSelectWidth={false} value={scenario} onChange={v=>{setScenario(v);setSteps([]);}} options={[{value:'nginx',label:'Nginx 502 / 504'},{value:'pool',label:'连接池等待超时'},{value:'container',label:'容器退出 / 重启'}]}/><Select aria-label="实际部署方式" style={{width:160}} popupMatchSelectWidth={false} value={deployment} onChange={v=>{setDeployment(v);setSteps([]);}} options={[{value:'unknown',label:'不知道部署方式'},{value:'docker',label:'Docker'},{value:'systemd',label:'Linux systemd'}]}/></Space>
    {deployment!=='unknown'&&<Input className="section-gap" aria-label="实际容器或服务名" value={target} onChange={e=>{setTarget(e.target.value);setSteps([]);}} placeholder="实际容器或服务名；不知道可以留空"/>}<Button className="section-gap" loading={busy} onClick={generate}>生成采集步骤</Button>
    {steps.map((s,i)=><Card size="small" className="section-gap" key={i} title={s.label}><p>{s.purpose}</p>{s.command&&<><pre>{s.command}</pre><Button onClick={()=>copyText(s.command).then(()=>message.success('已复制只读命令')).catch(()=>message.info('请手动选中并复制命令'))}>复制命令</Button></>}<p>{s.scenario_notes}</p><small>{s.risk}</small></Card>)}
  </Modal></>;
}
