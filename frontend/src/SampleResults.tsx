import { Collapse, Table } from 'antd';
import { formatTime } from './api';
import type { SampleChecks } from './types';

export default function SampleResults({value}: {value?: SampleChecks}) {
  if (!value?.measurements.length) return null;
  const compare = value.comparison;
  return <div className="section-gap">
    {compare && <section aria-label="处理前后对照">
      <h4>处理前后对照 · <code>{[compare.method, compare.endpoint].filter(Boolean).join(' ')}</code></h4>
      <div className="sample-comparison">
        <section><strong>处理前</strong>
          <p>{compare.before_requests === undefined ? '请求总数未记录' : compare.before_requests + ' 个有效样本'} · {compare.before_failed_samples} 个失败</p>
          {compare.before_from && compare.before_until && <small>{formatTime(compare.before_from)} 至 {formatTime(compare.before_until)}</small>}
        </section>
        <section><strong>处理后</strong>
          <p>{compare.after_requests} 个有效样本 · {compare.after_failures} 个失败</p>
          <small>{formatTime(compare.after_from)} 至 {formatTime(compare.after_until)}</small>
          {!!compare.after_duplicates && <small>合并了 {compare.after_duplicates} 条重复记录</small>}
        </section>
      </div>
    </section>}
    <Collapse size="small" items={[{key:'samples',label:'查看各份证据的统计',children:<Table size="small" pagination={false}
      rowKey="evidence_id" dataSource={value.measurements} scroll={{x:420}} columns={[
        {title:'证据',dataIndex:'evidence_id'}, {title:'识别记录',dataIndex:'request_count'}, {title:'HTTP失败',dataIndex:'failed_requests'},
        {title:'连接池异常行',render:(_, row) => row.pool_error_lines.length},
        {title:'容器状态',render:(_, row) => row.container_states.length ? '已识别' : '未提供'},
      ]} />}]}/>
    <p className="muted">{value.scope}。未提供或无法识别的记录不参与统计。</p>
  </div>;
}
