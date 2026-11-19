import { Collapse } from 'antd';
import type { AgentEvent } from './types';
import type { ReactNode } from 'react';

const tools:Record<string,string> = {search_runbook:'检索适用资料',search_saved_cases:'查询历史案例',get_site_overview:'读取网站背景',read_evidence_lines:'核对日志原文'};
function describe(event:AgentEvent) {
  if (event.type === 'tool_start') return tools[event.name || ''] || '开始检索相关资料';
  if (event.type === 'tool') return event.summary || '资料检索已返回';
  if (event.type === 'error') return event.message || '本次分析未完成';
  if (event.type === 'cancelled') return event.message || '本次分析已停止';
  if (event.type === 'final') return event.end_reason === 'answered' ? '本次分析已完成' : '本次分析未形成有效报告';
  if (event.type === 'existing_run') return '读取同一次请求的运行状态';
  return event.message || '分析阶段已更新';
}

export default function ProcessFold({phase, failed, events, startedAt, updatedAt, cancelling,action,children}: {
  phase:string; failed:boolean; events:AgentEvent[]; startedAt:number|null; updatedAt:number|null; cancelling:boolean;
  action?:ReactNode;children?:ReactNode;
}) {
  return <Collapse className="support-process support-analysis-details" size="small" items={[{
    key:'process', label:<span className="support-details-label"><strong>分析详情</strong>{phase&&<span className={failed ? 'support-phase failed' : 'support-phase'} role="status">{phase}</span>}</span>,
    extra:action&&<span onClick={event=>event.stopPropagation()}>{action}</span>,
    children:<>
      {startedAt !== null && updatedAt !== null && <p className="muted">开始于 {new Date(startedAt).toLocaleTimeString()} · 最后更新 {new Date(updatedAt).toLocaleTimeString()} · 截至最后更新 {Math.max(0,(updatedAt-startedAt)/1000).toFixed(1)} 秒</p>}
      {cancelling && <p className="muted">正在等待当前请求收尾，仍可能产生模型用量。</p>}
      {!!events.length&&<><h4>处理过程 · {events.length} 条记录</h4><ol>{events.map((event,index) => <li key={index}>{describe(event)}</li>)}</ol></>}
      {phase&&!events.length&&<p className="muted">尚未记录到工具或阶段事件。</p>}
      {children}
    </>,
  }]}/>;
}
