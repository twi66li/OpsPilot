import InfoTip from './InfoTip';
import { Alert, Button, Tag } from 'antd';
import { formatTime } from './api';
import type { EvidencePreview, EvidenceSummaryData } from './types';

export default function EvidenceSummary({value,onLines}:{value:EvidenceSummaryData;onLines?:(lines:number[])=>void}){
  const link=(label:string,lines:number[])=>onLines?<Button size="small" className="summary-line-link" onClick={()=>onLines(lines)}>{label} · L{lines.slice(0,5).join(', L')}{lines.length>5?'…':''}</Button>:<Tag>{label}</Tag>;
  return <section className="evidence-summary" aria-label="日志关键信息"><h4>这份日志里有什么 <InfoTip label="日志摘要说明">{value.notice}</InfoTip></h4>
    <p>{value.total_lines} 行文本 · {value.request_count?`识别到 ${value.request_count} 次HTTP请求，其中 ${value.failed_requests} 次失败`:'未识别到HTTP请求，不能当作零错误'}</p>
    <p className="result-detail">{value.time_range?`识别时间范围：${formatTime(value.time_range.from)} → ${formatTime(value.time_range.until)}`:'未识别到带时区的时间；请核对采集时间。'}</p>
    {!!value.http_statuses.length&&<div className="summary-links">{value.http_statuses.map(item=><span key={item.status}>{link(`HTTP ${item.status} · ${item.count} 次`,item.lines)}</span>)}</div>}
    {!!value.endpoints.length&&<div className="summary-links">{value.endpoints.slice(0,8).map((item,index)=><span key={index}>{link(`${item.method} ${item.endpoint||'未标明接口'} · ${item.count} 次`,item.lines)}</span>)}</div>}
    {!!value.exceptions.length&&<div className="summary-links">{value.exceptions.map(item=><span key={item.name}>{link(`${item.name} · ${item.count} 次`,item.lines)}</span>)}</div>}
    {!!value.repeated.length&&<div className="summary-links">{value.repeated.slice(0,5).map((item,index)=><span key={index}>{link(`相同日志内容重复 ${item.count} 次`,item.lines)}</span>)}</div>}
    {!!value.scenarios.length&&<div><strong>可参考的排障方向</strong>{value.scenarios.map(item=><div className="summary-links" key={item.id}><span>{item.title}</span>{item.signals.map(signal=><span key={signal.label}>{link(signal.label,signal.lines)}</span>)}</div>)}</div>}

  </section>;
}

export function RedactionNotice({value}:{value:EvidencePreview}){
  return <Alert className="section-gap" type="warning" title={`本次预览已替换 ${value.replacements} 处常见敏感内容`} description={<>
    <div>{value.findings?.map(item=><Tag key={item.label}>{item.label} {item.count} 处</Tag>)}</div>
    {!value.replacements&&<p>未匹配到预设敏感字段，仍需人工核对。</p>}
    {!!value.review_findings?.length&&<p>{value.review_findings.map(item=>`${item.label} ${item.count} 处`).join('；')}：为保留排障关联暂未替换，请检查后再提交。</p>}
    <p>{value.notice||'请检查预览内容，确认无误后再提交；编辑文本后需要重新预览。'}</p>
  </>}/>;
}
