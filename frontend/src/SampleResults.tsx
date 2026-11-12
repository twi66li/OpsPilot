import { Alert, Descriptions, Table } from 'antd';
import type { SampleChecks } from './types';

export default function SampleResults({value}: {value?: SampleChecks}) {
  if (!value?.measurements.length) return null;
  const compare = value.comparison;
  return <div className="section-gap"><h4>程序核对的提交样本</h4><Table size="small" pagination={false} rowKey="evidence_id" dataSource={value.measurements} columns={[
    {title:'证据',dataIndex:'evidence_id'}, {title:'识别请求',dataIndex:'request_count'}, {title:'HTTP失败',dataIndex:'failed_requests'},
    {title:'连接池异常行',render:(_, row) => row.pool_error_lines.length}, {title:'容器State',render:(_, row) => row.container_states.length ? '已识别' : '未提供'},
  ]} />{compare && <Descriptions className="section-gap" size="small" column={2} items={[
    {key:'target',label:'对照接口',children:`${compare.method} ${compare.endpoint}`}, {key:'before',label:'原失败样本',children:compare.before_failed_samples},
    {key:'after',label:'处理后请求',children:compare.after_requests}, {key:'errors',label:'处理后失败',children:compare.after_failures},
    {key:'from',label:'新样本开始',children:compare.after_from},{key:'until',label:'新样本结束',children:compare.after_until},
  ]} />}<Alert className="section-gap" type={value.verification.verdict === 'sample_recovered' ? 'success' : 'info'} title="样本核对结论" description={value.verification.reason} /><p className="muted">{value.scope}。未识别格式或未提供的内容不会按零错误处理；本统计不是线上流量和可用率。</p></div>;
}
