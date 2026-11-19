import InfoTip from './InfoTip';
import { Button, Card, Collapse, Tag } from 'antd';
import { formatTime } from './api';
import { siteEnvironments } from './SiteDependencies';
import type { EvidenceSpan, Hit, PersonalCase } from './types';

export default function ResultCard({record, hits, disabled, confirmPlan, onEvidence, onSource, showSummary=true}: {
  record: PersonalCase; hits: Hit[]; disabled: boolean; confirmPlan: () => void; showSummary?: boolean;
  onEvidence: (id: string, start: number, end: number) => void; onSource: (hit: Hit) => void;
}) {
  const report = record.report;
  if (!report) return null;
  // 仅为本记录已保存、行范围有效的日志提供依据入口。
  const observations = report.observations.flatMap(observation => {
    const evidence = record.evidence?.find(e => e.id === observation.evidence_id);
    return evidence && observation.line_start >= 1 && observation.line_end >= observation.line_start && observation.line_end <= evidence.text.split('\n').length
      ? [{observation, evidence}] : [];
  });
  const references = hits.filter(hit => hit.citation_id !== undefined && report.references.includes(hit.citation_id));
  const scope = report.evidence_scope;
  const renderSpans = (spans: EvidenceSpan[]) => spans.length ? spans.map(span => {
    const label = record.evidence?.find(item => item.id===span.evidence_id)?.label || '本次证据';
    return <Button size="small" className="result-reference" key={span.evidence_id+':'+span.line_start+':'+span.line_end}
      onClick={()=>onEvidence(span.evidence_id,span.line_start,span.line_end)}>{label} · L{span.line_start}–L{span.line_end}</Button>;
  }) : <span className="muted">尚无</span>;

  return <Card title="当前判断" className="support-result-card">
    {scope && <div className="result-scope" aria-label="调查范围">
      <span>{scope.site_name || '未关联网站'} · {siteEnvironments[scope.environment || 'unknown'] || '未分类'}</span>
      {scope.endpoint && <code>{scope.endpoint}</code>}
      <span>{scope.from && scope.until ? formatTime(scope.from)+' 至 '+formatTime(scope.until) : '样本时间未取得'}</span>
      {scope.sources.map(source=><Tag key={source}>{source==='scheduled_http_probe'?'定时 HTTP 巡检':'用户提交证据'}</Tag>)}
    </div>}
    {showSummary && <section aria-label="发现什么"><h4>发现什么</h4><p className="support-summary">{report.summary}</p></section>}
    <section aria-label="依据"><h4>依据</h4>
      {observations.length ? observations.map(({observation:o,evidence},index) => <div className="result-observation" key={index}>
        <p>{o.meaning}</p><Button className="result-reference" onClick={() => onEvidence(evidence.id,o.line_start,o.line_end)}>{evidence.label} · L{o.line_start}–L{o.line_end}</Button>
      </div>) : <p className="result-detail">当前记录中没有可核对的已提交日志行，不能据此确认日志中的故障原因。</p>}
      {references.map(hit => <Button key={hit.chunk_id} className="result-reference" onClick={() => onSource(hit)}>[{hit.citation_id}] {hit.source_kind === 'official' ? '官方资料' : '网站手册'} · {hit.doc_title}{hit.version ? ` v${hit.version}` : ''}</Button>)}
      {!!report.references.length && !references.length && <p className="result-detail">报告引用的资料出处暂不可回查。</p>}
      {!!references.length && <InfoTip label="报告参考资料说明">手册是参考依据，不是本次故障的实时证据。</InfoTip>}
    </section>
    <section aria-label="下一步"><h4>下一步 <InfoTip label="检查步骤说明">由你在自己的环境操作；展开步骤可查看命令、预期现象、影响与回退。</InfoTip></h4>
      {report.steps.length ? <>
        
        <Collapse className="result-steps" items={report.steps.map((step,index) => ({key:String(index),label:<span>第 {index+1} 步 · {step.location} <Tag color={step.risk === '只读' ? 'green' : 'orange'}>{step.risk}</Tag></span>,children:<>
          <pre className="source-text">{step.action}</pre><p><strong>正常时：</strong>{step.normal}<br/><strong>异常时：</strong>{step.abnormal}</p>{step.rollback && <p><strong>影响与回退：</strong>{step.rollback}</p>}
        </>}))}/>
        <Button disabled={disabled || record.plan?.revision === record.revision} onClick={confirmPlan}>我确认检查方案，准备自行操作</Button>
        <InfoTip label="确认方案说明">此按钮只记录你的确认，不会执行命令、修改配置或标记恢复。</InfoTip>
      </> : <p className="result-detail">本次没有给出可执行的检查步骤，可先补充下方缺失信息。</p>}
    </section>
    <section aria-label="还不确定"><h4>还不确定</h4>
      {!!report.hypotheses.length && <><strong>待验证的可能原因</strong><ul>{report.hypotheses.map((value,index) =>
        typeof value==='string' ? <li key={index}>{value}</li> : <li key={index} className="result-hypothesis">
          <strong>{value.cause}</strong>
          <div>支持证据 · {renderSpans(value.supporting)}</div>
          <div>反对证据 · {renderSpans(value.contradicting)}</div>
          <div>待补充 · {value.missing.length ? value.missing.join('、') : '暂无记录'}</div>
        </li>)}</ul></>}
      {!!report.missing_info.length && <><strong>下一步最需要补充</strong><ul>{report.missing_info.map((value,index) => <li key={index}>{value}</li>)}</ul></>}
      {!report.hypotheses.length && !report.missing_info.length && <p className="result-detail">报告未列出其他待验证项；这不代表故障已解决。</p>}
    </section>
  </Card>;
}
