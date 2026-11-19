import InfoTip from './InfoTip';
import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Descriptions, Modal, Space, Tag } from 'antd';
import { api } from './api';
import type { Hit, ScenarioGuide, SupportEntry } from './types';

export default function ScenarioGuides({onSource,assist}: {onSource:(hit:Hit)=>void;assist:(entry:SupportEntry)=>void}) {
  const {message} = App.useApp(), [guides,setGuides] = useState<ScenarioGuide[]>([]), [selected,setSelected] = useState<ScenarioGuide|null>(null);
  useEffect(() => {api<ScenarioGuide[]>('/api/support/scenarios').then(setGuides).catch(e => message.error(e.message));}, [message]);
  useEffect(() => {
    if (!selected) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setSelected(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected]);
  return <><div className="section-bottom info-inline-label"><span>按常见异常症状选择场景，先执行只读检查再判断是否需要变更</span><InfoTip label="场景指南说明">先按异常现象选场景，再按实际结果排查。示例日志仅用于学习，不会自动进入你的网站证据。</InfoTip></div>
    <div className="home-cards">{guides.map(g => <Card key={g.id} title={g.title}><Space wrap>{g.components.map(c=><Tag key={c}>{c}</Tag>)}</Space><p>{g.description}</p><ul>{g.symptoms.map(s=><li key={s}>{s}</li>)}</ul><div className="card-actions"><Button onClick={()=>setSelected(g)}>查看检查、分支与结果样例</Button><Button type="primary" onClick={()=>assist({workflow:"diagnose",question:`请按「${g.title}」指导我排查。请先说明应收集哪些现场证据；我尚未提交自己网站的日志。`})}>按此场景提问</Button></div></Card>)}</div>
    <Modal open={!!selected} keyboard title={selected?.title} width={820} onCancel={()=>setSelected(null)} footer={<Button onClick={()=>setSelected(null)}>返回资料库</Button>}>
      {selected && <><p>{selected.description}</p>{selected.checks.map((s,i)=><Card size="small" className="section-bottom" key={i} title={`检查 ${i+1} · ${s.risk}`}><Descriptions column={1} items={[{key:'where',label:'位置',children:s.where},{key:'action',label:'动作',children:<pre>{s.action}</pre>},{key:'normal',label:'正常结果',children:s.normal},{key:'abnormal',label:'异常分支',children:s.abnormal}]} /></Card>)}
        <Alert type="warning" title="修改前了解影响和回退" description={selected.change_guidance} /><h4>如何核对结果</h4><p>{selected.verification}</p>
        <Alert type="info" title="下面是教学样例，非网站实采。成功样例也不能证明生产长期恢复。" /><h4>异常样例</h4><pre className="source-text">{selected.sample_before}</pre><h4>处理后的请求样例</h4><pre className="source-text">{selected.sample_after}</pre>
        <Space wrap><a href={selected.source} target="_blank" rel="noreferrer">官方来源</a><Button onClick={()=>api<Hit>(`/api/support/library/${selected.doc_id}`).then(h=>onSource({...h,chunk_id:''})).catch(e=>message.error(e.message))}>阅读完整指南与适用版本</Button></Space>
      </>}
    </Modal>
  </>;
}
