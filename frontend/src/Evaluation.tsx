import { useEffect, useState } from 'react';
import { Alert, Card, Checkbox, Collapse, Empty, Input, Select, Space, Spin, Table, Tag } from 'antd';
import { api } from './api';
import type { EvalQuestion, EvalReport } from './types';

type EvalRun = {id: string; label: string; category: string; question_count: number};

export default function Evaluation() {
  const [runs, setRuns] = useState<EvalRun[]>([]), [runId, setRunId] = useState(''), [report, setReport] = useState<(EvalReport & {details?: unknown}) | null>(null), [query, setQuery] = useState('');
  const [development, setDevelopment] = useState(false), [developmentCount, setDevelopmentCount] = useState(0), [loading, setLoading] = useState(false), [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    api<{runs: EvalRun[]; default_run: string; development_count: number}>(`/api/eval/runs?include_development=${development}`).then(result => {if (active) {
      setRuns(result.runs); setDevelopmentCount(result.development_count);
      setRunId(previous => result.runs.some(r => r.id === previous) ? previous : result.default_run);
    }}).catch(e => {if (active) setError(e.message);}).finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [development]);
  useEffect(() => {
    if (!runId) {setReport(null); return;} let active = true; setReport(null); setLoading(true); setError('');
    api<EvalReport & {details?: unknown}>(`/api/eval/results?run=${encodeURIComponent(runId)}`).then(value => {if (active) setReport(value);}).catch(e => {if (active) setError(e.message);}).finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [runId]);
  const questions = report?.questions.filter(item => `${item.id} ${item.question}`.toLowerCase().includes(query.toLowerCase())) || [];
  return <div className="page-stack"><div className="page-title"><div><h2>评测记录</h2><p>查看已有案例的回答、证据、程序检查和模型用量。</p></div></div>
    <Alert type="warning" title="这些结果来自开发与修复案例，不代表独立根因准确率。缺少人工标注或裁判结论时，保留缺失状态。" />
    <Checkbox checked={development} onChange={e => setDevelopment(e.target.checked)}>显示开发 / 流程验收记录（{developmentCount}）</Checkbox>
    <Space wrap><Select aria-label="评测运行" placeholder="选择评测运行" style={{minWidth: 360}} value={runId || undefined} onChange={setRunId} options={runs.map(item => ({value: item.id, label: `${item.label} · ${item.question_count}题 · ${item.id}`}))} />
      <Input aria-label="筛选评测问题" value={query} onChange={e => setQuery(e.target.value)} placeholder="筛选问题或编号" allowClear /></Space>
    {error && <Alert type="error" title="评测记录加载失败" description={error} />}
    {loading && <Spin tip="正在读取评测记录"><div style={{height:40}} /></Spin>}
    {!loading && !error && !runs.length && <Empty description="尚无可展示的逐题评测。可勾选上方选项查看已有开发 / 流程验收记录。" />}
    {report && <>
      <Card size="small"><Space wrap><Tag color="blue">{runs.find(r => r.id === runId)?.label}</Tag><strong>{report.questions.length} 条逐题记录</strong><span className="muted">展开问题可查看回答、依据和用量；这些历史样本不代表当前线上准确率。</span></Space></Card>
      {Object.keys(report.summary || {}).length > 0 && <Collapse items={[{key: 'summary', label: '运行汇总与统计口径', children: <pre>{JSON.stringify(report.summary, null, 2)}</pre>}]} />}
      {!report.questions.length ? <><Alert type="info" title="这是一份流程验收记录，没有逐题评测明细。" description="它用于检查功能流程，不参与逐题评测对比。可展开查看原始验收结果。" /><Collapse items={[{key:'details',label:'原始流程验收记录',children:<pre>{JSON.stringify(report.details, null, 2)}</pre>}]} /></> :
      <Card><Table<EvalQuestion> rowKey="id" dataSource={questions} pagination={{pageSize: 10}} columns={[
        {title: '编号', dataIndex: 'id', width: 130}, {title: '问题', dataIndex: 'question'},
        {title: '状态', dataIndex: 'status', render: value => <Tag>{value || '未记录'}</Tag>},
        {title: '耗时', dataIndex: 'elapsed_s', render: value => typeof value === 'number' ? `${value.toFixed(1)}s` : '未记录'},
      ]} expandable={{expandedRowRender: item => <div className="page-stack"><pre className="source-text">{item.answer || '尚无回答'}</pre><Collapse items={[
        {key: 'checks', label: '程序检查与裁判结果', children: <pre>{JSON.stringify({program_checks: item.program_checks, judges: item.judges}, null, 2)}</pre>},
        {key: 'usage', label: '模型用量', children: <pre>{JSON.stringify({usage: item.usage, agent_usage: item.agent_usage}, null, 2)}</pre>},
        {key: 'evidence', label: '依据', children: <pre>{JSON.stringify(item.evidence, null, 2)}</pre>},
      ]} /></div>}} /></Card>}
    </>}
  </div>;
}
