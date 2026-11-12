import { useEffect, useState } from 'react';
import { App, Button, Card, Input, Space, Tag } from 'antd';
import { post } from './api';
import type { PersonalCase } from './types';

export default function PlanChecklist({record, disabled, changed, evidence}: {record: PersonalCase; disabled: boolean; changed: (record: PersonalCase) => void; evidence: (step: number) => void}) {
  const {message} = App.useApp(), [notes,setNotes] = useState<string[]>([]), [busy,setBusy] = useState<number|null>(null);
  useEffect(() => {setNotes((record.plan?.steps || []).map((_, i) => record.plan?.checks?.[i]?.note || ''));}, [record.plan]);
  if (!record.plan) return null;
  const plan = record.plan;
  async function save(index: number, status: string) {
    setBusy(index); try {changed(await post<PersonalCase>(`/api/support/cases/${record.id}/plan/steps/${index}`, {revision:plan.revision,status,note:notes[index] || ''}));} catch(e) {message.error((e as Error).message);} finally {setBusy(null);}
  }
  return <Card title={`已确认方案 · 第${plan.revision}次分析`} size="small"><p className="muted">逐项记录自己做过的检查，提交结果仍需单独添加证据；完成标记不会自动确认恢复。</p>{plan.steps.map((s,i) => <div className="check-step" key={i}><Space><strong>检查 {i+1} · {s.location}</strong><Tag>{({pending:'未检查',done:'用户已检查',skipped:'暂未执行'} as Record<string,string>)[plan.checks?.[i]?.status || 'pending']}</Tag></Space><pre>{s.action}</pre><Input.TextArea aria-label={`检查${i+1}的结果记录`} disabled={disabled} rows={2} value={notes[i] || ''} onChange={e => setNotes(previous => previous.map((n,j) => j === i ? e.target.value : n))} placeholder="我检查到了什么，或为什么暂未执行" /><Space className="section-gap" wrap><Button size="small" disabled={disabled} loading={busy===i} onClick={() => save(i,'done')}>记录已检查</Button><Button size="small" disabled={disabled} onClick={() => save(i,'skipped')}>记录暂未执行</Button><Button size="small" disabled={disabled} onClick={() => evidence(i)}>提交此步的新证据</Button></Space></div>)}</Card>;
}
