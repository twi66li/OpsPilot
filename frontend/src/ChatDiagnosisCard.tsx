import { useRef, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Collapse, Drawer, Empty, Input, Modal, Space, Tag } from 'antd';
import { post } from './api';
import type { ChatMessage, Hit, PersonalCase } from './types';
import { verdictLabels } from './supportTypes';
import StatusTag from './StatusTag';
import ResultCard from './ResultCard';
import PlanChecklist from './PlanChecklist';
import SampleResults from './SampleResults';
import EvidenceSummary from './EvidenceSummary';

export default function ChatDiagnosisCard({
  record,
  redaction,
  running,
  phase,
  disabled,
  onStop,
  onAddEvidence,
  onChanged,
  onSource,
  onDetail,
}: {
  record: PersonalCase;
  redaction?: NonNullable<ChatMessage['meta']>['redaction'];
  running: boolean;
  phase?: string;
  disabled: boolean;
  onStop?: () => void;
  onAddEvidence: (step?: number) => void;
  onChanged: (next?: PersonalCase) => void;
  onSource: (hit: Hit) => void;
  onDetail: (caseId: string) => void;
}) {
  const {message} = App.useApp();
  const [actionBusy, setActionBusy] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [focus, setFocus] = useState<{id: string; start: number; end: number; lines?: number[]} | null>(null);
  const [activeKeys, setActiveKeys] = useState<string[]>([]);
  const [resolveOpen, setResolveOpen] = useState(false);
  const [solution, setSolution] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const focusRef = useRef<HTMLSpanElement | null>(null);

  const report = record.report;
  const hits = record.report_hits || [];
  const sampleChecks = record.sample_checks || report?.program_checks;
  const verification = sampleChecks?.verification || report?.verification;
  const resultReady = verification?.verdict === 'sample_recovered';
  const readOnly = disabled || !!record.archived || record.status === 'user_resolved';
  const status = record.status === 'sample_recovered' && !resultReady ? 'awaiting_results' : record.status;

  async function confirmPlan() {
    setActionBusy(true);
    try {
      await post(`/api/support/cases/${record.id}/confirm-plan`);
      message.success('已确认检查方案');
      onChanged();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setActionBusy(false);
    }
  }

  async function markResolved() {
    setActionBusy(true);
    try {
      await post(`/api/support/cases/${record.id}/status`, {
        status: 'user_resolved',
        note: solution,
        user_confirmed: confirmed,
      });
      message.success('已标记为解决');
      setResolveOpen(false);
      onChanged();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setActionBusy(false);
    }
  }

  const evidenceCount = (record.evidence?.length || 0) + hits.length;

  return (
    <>
      <Card
        size="small"
        className="chat-diagnosis-card"
        title={
          <Space wrap size="small">
            <strong>证据诊断</strong>
            <StatusTag status={status || 'investigating'} />
            {record.profile?.name && <Tag>{record.profile.name}</Tag>}
          </Space>
        }
        extra={
          <Space size="small">
            <Button size="small" onClick={() => {setFocus(null); setDrawerOpen(true);}}>
              证据与资料 · {evidenceCount}
            </Button>
            <Button size="small" type="link" onClick={() => onDetail(record.id)}>
              查看详情
            </Button>
          </Space>
        }
      >
        <div className="page-stack">
          {redaction && (redaction.replacements > 0 || (redaction.review_findings?.length || 0) > 0) && (
            <small className="muted">
              {redaction.replacements > 0 ? `已自动遮蔽 ${redaction.replacements} 处敏感信息` : '未检出常见密钥'}
              {!!redaction.review_findings?.length && ` · 含 ${redaction.review_findings.reduce((n, f) => n + f.count, 0)} 处 IP 请留意`}
            </small>
          )}
          {running && (
            <Alert
              type="info"
              title={phase || '正在结合日志与资料诊断…'}
              action={onStop ? <Button size="small" danger onClick={onStop}>停止</Button> : undefined}
            />
          )}
          {report && (
            <ResultCard
              record={record}
              hits={hits}
              disabled={running || actionBusy || readOnly}
              confirmPlan={confirmPlan}
              onSource={onSource}
              onEvidence={(id, start, end) => {
                setFocus({id, start, end});
                setActiveKeys([id]);
                setDrawerOpen(true);
              }}
            />
          )}
          {record.plan && (
            <PlanChecklist
              record={record}
              disabled={running || actionBusy || readOnly}
              changed={next => onChanged(next)}
              evidence={step => onAddEvidence(step)}
            />
          )}
          {verification && (
            <Alert
              type={resultReady ? 'success' : 'info'}
              title={verdictLabels[verification.verdict] || verification.verdict}
              description={verification.reason}
            />
          )}
          <SampleResults value={sampleChecks} />
          {!readOnly && (
            <Space wrap>
              {resultReady && (
                <Button
                  type="primary"
                  size="small"
                  disabled={running || actionBusy}
                  onClick={() => {setSolution(record.solution || ''); setConfirmed(false); setResolveOpen(true);}}
                >
                  确认已解决
                </Button>
              )}
              <Button
                type={resultReady ? 'default' : 'primary'}
                size="small"
                disabled={running || actionBusy}
                onClick={() => onAddEvidence()}
              >
                补充证据
              </Button>
            </Space>
          )}
        </div>
      </Card>

      <Drawer
        title="诊断证据与参考资料"
        width={430}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        afterOpenChange={open => {
          const line = focusRef.current, pre = line?.parentElement;
          if (open && line && pre) {
            pre.scrollTop = line.offsetTop - 24;
            line.focus({preventScroll: true});
          }
        }}
      >
        <div className="page-stack">
          <Card size="small" title={`提交证据 · ${record.evidence?.length || 0}`}>
            {!record.evidence?.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无证据" />}
            <Collapse
              activeKey={activeKeys}
              onChange={keys => setActiveKeys(Array.isArray(keys) ? keys : [keys])}
              items={(record.evidence || []).map(e => ({
                key: e.id,
                label: <>{e.label}<small> · {e.kind === 'check_result' ? '复测结果' : '初始/补充证据'}</small></>,
                children: (
                  <>
                    <small>{e.id} · {e.collected_at || '未填写时间'}</small>
                    {e.summary && (
                      <EvidenceSummary
                        value={e.summary}
                        onLines={lines => {
                          if (lines.length) setFocus({id: e.id, start: Math.min(...lines), end: Math.max(...lines), lines});
                        }}
                      />
                    )}
                    <pre className="evidence-lines" aria-label={`${e.label}原文`}>
                      {e.text.split('\n').map((line, index) => {
                        const highlighted =
                          focus?.id === e.id &&
                          (focus.lines ? focus.lines.includes(index + 1) : index + 1 >= focus.start && index + 1 <= focus.end);
                        const first = highlighted && index + 1 === focus?.start;
                        return (
                          <span
                            key={index}
                            className={`evidence-line${highlighted ? ' highlighted' : ''}`}
                            ref={first ? focusRef : undefined}
                            tabIndex={first ? -1 : undefined}
                          >
                            {`L${index + 1}: ${line || ' '}`}
                          </span>
                        );
                      })}
                    </pre>
                  </>
                ),
              }))}
            />
          </Card>
          <Card size="small" title="参考资料">
            {!hits.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无引用资料" />}
            {hits.map(h => (
              <button key={h.chunk_id} className="evidence-card" onClick={() => onSource(h)}>
                <strong>[{h.citation_id}] {h.doc_title}</strong>
                <span>查看原文 →</span>
              </button>
            ))}
          </Card>
        </div>
      </Drawer>

      <Modal
        open={resolveOpen}
        title="确认问题已解决"
        onCancel={() => setResolveOpen(false)}
        onOk={markResolved}
        confirmLoading={actionBusy}
        okText="确认解决"
        okButtonProps={{disabled: !confirmed || !solution.trim()}}
      >
        <Input.TextArea
          aria-label="处理记录"
          value={solution}
          maxLength={2000}
          rows={4}
          onChange={e => setSolution(e.target.value)}
          placeholder="记录实际做过的修复操作与验证结果…"
        />
        <Checkbox className="section-gap" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>
          我已确认实际问题已解决
        </Checkbox>
      </Modal>
    </>
  );
}
