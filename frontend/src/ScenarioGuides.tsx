import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Descriptions, Modal, Space, Tag } from 'antd';
import { api } from './api';
import type { Hit } from './types';

interface Guide {id:string; title:string; doc_id:string; components:string[]; description:string; symptoms:string[]; checks:{where:string;action:string;normal:string;abnormal:string;risk:string}[]; change_guidance:string; verification:string; sample_before:string; sample_after:string; source:string}
export default function ScenarioGuides({onSource}: {onSource:(hit:Hit)=>void}) {
  const {message} = App.useApp(), [guides,setGuides] = useState<Guide[]>([]), [selected,setSelected] = useState<Guide|null>(null);
  useEffect(() => {api<Guide[]>('/api/support/scenarios').then(setGuides).catch(e => message.error(e.message));}, [message]);
  return <><Alert className="section-bottom" type="info" title="先按异常现象选场景，再按自己网站的实际结果排查。示例日志仅用于学习，不会自动进入你的网站证据。" />
    <div className="home-cards">{guides.map(g => <Card key={g.id} title={g.title}><Space wrap>{g.components.map(c=><Tag key={c}>{c}</Tag>)}</Space><p>{g.description}</p><ul>{g.symptoms.map(s=><li key={s}>{s}</li>)}</ul><Button onClick={()=>setSelected(g)}>查看检查、分支与结果样例</Button></Card>)}</div>
    <Modal open={!!selected} title={selected?.title} width={820} onCancel={()=>setSelected(null)} footer={<Button onClick={()=>setSelected(null)}>返回资料库</Button>}>
      {selected && <><p>{selected.description}</p>{selected.checks.map((s,i)=><Card size="small" className="section-bottom" key={i} title={`检查 ${i+1} · ${s.risk}`}><Descriptions column={1} items={[{key:'where',label:'位置',children:s.where},{key:'action',label:'动作',children:<pre>{s.action}</pre>},{key:'normal',label:'正常结果',children:s.normal},{key:'abnormal',label:'异常分支',children:s.abnormal}]} /></Card>)}
        <Alert type="warning" title="修改前了解影响和回退" description={selected.change_guidance} /><h4>如何核对结果</h4><p>{selected.verification}</p>
        <Alert type="info" title="下面是教学样例，非网站实采。成功样例也不能证明生产长期恢复。" /><h4>异常样例</h4><pre className="source-text">{selected.sample_before}</pre><h4>处理后的请求样例</h4><pre className="source-text">{selected.sample_after}</pre>
        <Space wrap><a href={selected.source} target="_blank" rel="noreferrer">官方来源</a><Button onClick={()=>api<Hit>(`/api/support/library/${selected.doc_id}`).then(h=>onSource({...h,chunk_id:''})).catch(e=>message.error(e.message))}>阅读完整指南与适用版本</Button></Space>
      </>}
    </Modal>
  </>;
}
