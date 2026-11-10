import { useEffect, useState } from 'react';
import { Alert, App, Card, Collapse, Input, Select, Space, Table, Tag } from 'antd';
import { api } from './api';
import type { EvalQuestion, EvalReport } from './types';

export default function Evaluation() {
  const {message} = App.useApp();
  const [runs, setRuns] = useState<{id: string}[]>([]), [runId, setRunId] = useState(''), [report, setReport] = useState<EvalReport | null>(null), [query, setQuery] = useState('');
  useEffect(() => {
    let active = true;
    api<{runs: {id: string}[]}>('/api/eval/runs').then(result => {if (active) {setRuns(result.runs); setRunId(result.runs.at(-1)?.id || '');}}).catch(e => {if (active) message.error(e.message);});
    return () => {active = false;};
  }, [message]);
  useEffect(() => {
    if (!runId) return; let active = true; setReport(null);
    api<EvalReport>(`/api/eval/results?run=${encodeURIComponent(runId)}`).then(value => {if (active) setReport(value);}).catch(e => {if (active) message.error(e.message);});
    return () => {active = false;};
  }, [runId, message]);
  const questions = report?.questions.filter(item => `${item.id} ${item.question}`.toLowerCase().includes(query.toLowerCase())) || [];
  return <div className="page-stack"><div className="page-title"><div><h2>评测记录</h2><p>查看已有案例的回答、证据、程序检查和模型用量。</p></div></div>
    <Alert type="warning" title="这些结果来自开发与修复案例，不代表独立根因准确率。缺少人工标注或裁判结论时，保留缺失状态。" />
    <Space wrap><Select aria-label="评测运行" placeholder="选择评测运行" style={{minWidth: 360}} value={runId || undefined} onChange={setRunId} options={runs.map(item => ({value: item.id, label: item.id}))} />
      <Input aria-label="筛选评测问题" value={query} onChange={e => setQuery(e.target.value)} placeholder="筛选问题或编号" allowClear /></Space>
    {report && <>
      <Collapse items={[{key: 'summary', label: '运行汇总与统计口径', children: <pre>{JSON.stringify(report.summary, null, 2)}</pre>}]} />
      <Card><Table<EvalQuestion> rowKey="id" dataSource={questions} pagination={{pageSize: 10}} columns={[
        {title: '编号', dataIndex: 'id', width: 130}, {title: '问题', dataIndex: 'question'},
        {title: '状态', dataIndex: 'status', render: value => <Tag>{value || '未记录'}</Tag>},
        {title: '耗时', dataIndex: 'elapsed_s', render: value => typeof value === 'number' ? `${value.toFixed(1)}s` : '未记录'},
      ]} expandable={{expandedRowRender: item => <div className="page-stack"><pre className="source-text">{item.answer || '尚无回答'}</pre><Collapse items={[
        {key: 'checks', label: '程序检查与裁判结果', children: <pre>{JSON.stringify({program_checks: item.program_checks, judges: item.judges}, null, 2)}</pre>},
        {key: 'usage', label: '模型用量', children: <pre>{JSON.stringify({usage: item.usage, agent_usage: item.agent_usage}, null, 2)}</pre>},
        {key: 'evidence', label: '依据', children: <pre>{JSON.stringify(item.evidence, null, 2)}</pre>},
      ]} /></div>}} /></Card>
    </>}
  </div>;
}
