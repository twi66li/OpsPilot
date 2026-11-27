export interface Auth { access_token: string; username: string; role: string; tenant_id: string; is_guest?:boolean }
export interface Session { id: string; title: string; created_at: string }
export interface Hit {
  score?: number;
  chunk_id: string; doc_id: string; doc_title: string; text: string; source: string;
  version_id?: string; version?: number; page_start?: number; page_end?: number; heading_path?: string[]; citation_id?: number;
  parent_text?: string; file_url?: string; content_type?: string; matched_text?: string; source_name?: string;
  source_kind?: 'official' | 'private'; applicable_version?: string; checked_at?: string; content_hash?: string;
}
export interface ChatMessage {
  role: string;
  content: string;
  created_at?: string;
  meta?: {
    run_id?: string;
    citations?: string[];
    hits?: Hit[];
    case_id?: string;
    evidence_id?: string;
    workflow?: Workflow;
    prompt?: string;
    personal_model?: boolean;
    redaction?: {
      replacements: number;
      findings?: EvidencePreview['findings'];
      review_findings?: EvidencePreview['review_findings'];
    };
  };
}
export interface SessionHistory {
  title: string;
  messages: ChatMessage[];
  read_only?: boolean;
  active_run_id?: string | null;
  cases?: Record<string, PersonalCase & {active_run_id?: string | null}>;
}
export interface Approval { pending?: boolean; approval_id: string; ticket_draft: Record<string, unknown>; status?: string; created_at?: string }
export interface AgentEvent {
  type: string; answer?: string; delta?: string; message?: string; node?: string; name?: string; agent?: string;
  at?: string;
  summary?: string; run_id?: string; hits?: Hit[]; approval?: Approval; approval_id?: string;
  ticket_draft?: Record<string, unknown>; result?: Record<string, unknown>; end_reason?: string;
  support_report?: SupportReport;
  workflow?: string;
  status?: string;
}
export interface Run {
  run_id: string; status: string; created_at?:string; updated_at?:string;
  data: { question?:string; result?: AgentEvent; demo_operation_id?: string; events: { kind: string; at?:string; detail: AgentEvent | Record<string, unknown> }[] };
}
export interface KnowledgeDoc { id: string; title: string; source: string; chunk_count: number; active_version_id?: string }
export interface Version {
  id: string; version_id: string; doc_id: string; number: number; filename: string; content_hash: string;
  parser: string; status: string; progress: number; error: string; warnings: string[]; active: boolean;
  index_config: { chunk_count?: number }; pages?: {page: number | null; text: string}[]; chunks?: Hit[];
}
export interface Ticket { id: string; title: string; severity: string; status: string; description?: string; created_at: string }
export interface Remediation {
  id: string; run_id: string; action: string; action_label: string; service: string; reason: string;
  status: string; requested_by: string; decided_by: string; comment: string; result: Record<string, unknown>;
}
export interface EvalQuestion {
  id: string; question: string; answer: string; status: string; elapsed_s?: number;
  usage: Record<string, unknown>; program_checks?: Record<string, unknown>; judges?: Record<string, unknown>;
  evidence: unknown[]; agent_usage?: Record<string, unknown>;
}
export interface EvalReport { id: string; summary: Record<string, unknown>; questions: EvalQuestion[] }
export interface Site {
  url?:string;health_check?:{state:string;http_status?:number;collected_at:string;source_url:string;note?:string;scope:string};
  id: string; name: string; framework: string; deployment: string; database: string; versions: string; notes: string; doc_ids: string[];
  problem_count?: number; open_count?: number; last_check_at?:string;last_activity_at?:string; document_tasks?:{failed:number;processing:number};
  environment?:string; group?:string; owner?:string;
  dependencies?:{source:string;target:string;note?:string}[];
  http_monitor?: HttpMonitor;
  probe_history?: ProbePoint[];
  webhook?: {enabled:boolean; token_prefix?:string; created_at?:string; received_count?:number; last_received_at?:string};
}
export interface ProbePoint {
  collected_at:string; elapsed_ms?:number|null; http_status?:number; state:string; timed_out?:boolean; source_kind?:string;
}
export interface HttpMonitor {
  enabled:boolean; interval_minutes:number; failure_threshold:number; consecutive_failures?:number;
  next_check_at?:string|null; case_id?:string|null;
  last_result?:{state:string;http_status?:number;timed_out?:boolean;collected_at:string;note?:string;elapsed_ms?:number}|null;
  probe_history?: ProbePoint[];
}
export interface PreflightCheck { key:string; label:string; status:'pass'|'warn'|'fail'; detail:string }
export interface PreflightResult { passed:boolean; risk_level:string; target:string; rollback_hint:string; summary:string; checks:PreflightCheck[] }
export type Workflow = 'auto' | 'consult' | 'diagnose' | 'review' | 'history';
export interface SupportEntry {caseId?: string; siteId?: string; workflow?: Workflow; question?: string}
export interface SubmittedEvidence { id: string; label: string; kind: 'initial' | 'check_result'; text: string; collected_at: string; submitted_at: string; endpoint?: string; step_index?: number; plan_revision?: number; summary?: EvidenceSummaryData; source_kind?:string }
export interface EvidenceSummaryData {
  total_lines:number;request_count:number;failed_requests:number;
  time_range:{from:string;until:string;recognized:number}|null;
  http_statuses:{status:number;count:number;lines:number[]}[];
  endpoints:{method:string;endpoint:string;count:number;failures:number;lines:number[]}[];
  exceptions:{name:string;count:number;lines:number[]}[];
  repeated:{count:number;lines:number[]}[];
  scenarios:{id:string;title:string;doc_id:string;collection_key:string;signals:{label:string;count:number;lines:number[]}[]}[];
  notice:string;
}
export interface EvidencePreview {
  text:string;replacements:number;filename?:string;notice?:string;summary?:EvidenceSummaryData;
  findings?:{label:string;count:number;lines:number[]}[];
  review_findings?:{label:string;count:number;lines:number[]}[];
}
export interface ScenarioGuide {
  id:string;title:string;doc_id:string;components:string[];description:string;symptoms:string[];
  checks:{where:string;action:string;normal:string;abnormal:string;risk:string}[];
  change_guidance:string;verification:string;sample_before:string;sample_after:string;source:string;
  collection_key:string;collection_label:string;
}
export interface SampleChecks {
  scope: string; measurements: {evidence_id:string; request_count:number; failed_requests:number; pool_error_lines:number[]; container_states:Record<string,unknown>[]; requests:{endpoint:string; status:number; at:string; elapsed_ms:number|null}[]}[];
  comparison?: {
    method:string; endpoint:string; before_failed_samples:number;
    before_requests?:number; before_from?:string; before_until?:string;
    after_requests:number; after_failures:number; after_duplicates?:number; after_from:string; after_until:string;
  };
  verification: {verdict:string; evidence_ids:string[]; reason:string; method?:string};
}
export interface CheckStep { location: string; action: string; normal: string; abnormal: string; risk: string; rollback: string }
export interface EvidenceSpan { evidence_id:string; line_start:number; line_end:number; quote:string }
export interface SupportReport {
  summary: string; observations: { evidence_id: string; line_start: number; line_end: number; meaning: string; quote: string }[];
  hypotheses: (string | {cause:string; supporting:EvidenceSpan[]; contradicting:EvidenceSpan[]; missing:string[]})[];
  missing_info: string[]; steps: CheckStep[]; references: number[];
  verification: { verdict: string; evidence_ids: string[]; reason: string; method?:string };
  workflow?: string; program_checks?: SampleChecks;
  evidence_scope?: {site_name?:string; environment?:string; endpoint?:string; sources:string[]; from?:string; until?:string};
}
export interface PersonalCase {
  severity?:string; labels?:string[]; updated_at?:string;
  id: string; title?: string; site_id?: string; status: string; created_at: string; saved?: boolean; profile?: Partial<Site>;
  archived?: boolean; draft?: boolean; kind?:string; site_name?:string; summary?:string; sample_checks?: SampleChecks;
  problem?: {symptom?:string; endpoint?:string; impact?:string; started_at?:string; recent_changes?:string};
  evidence?: SubmittedEvidence[]; report?: SupportReport; report_hits?: Hit[]; revision?: number; last_run_id?: string;
  plan?: { revision: number; evidence_ids: string[]; steps: CheckStep[]; checks?: {status:string; note:string}[] }; solution?: string;
  reports?: {revision: number; report: SupportReport; hits: Hit[]; run_id: string}[];
  related_cases?: {session_id:string; solution:string; outcome:string; archived:boolean}[];
  handoff?: HandoffStatus;
}
export interface HandoffStatus {
  recipient:string; note:string; shared_at:string; status:'pending'|'reviewed'|'changes_requested';
  review_note?:string; reviewed_at?:string;
}
export interface ReceivedHandoff extends HandoffStatus {
  case_id:string; version:string; sender:string;
  snapshot:{title:string;site_name?:string;problem:PersonalCase['problem'];summary:string;
    evidence:Pick<SubmittedEvidence,'id'|'label'|'text'|'collected_at'|'source_kind'>[];
    verification:SampleChecks['verification']};
}

export interface PageData<T> {items:T[];total:number;page:number;page_size:number}
export interface MaintenanceTodo {
  id:string;title:string;state:string;url:string;
  kind?:'case'|'document'|'run'|'approval'|'completed';resource_id?:string;
  action?:'continue_case'|'submit_result'|'retry_document'|'resume_analysis'|'view_run'|'review_approval';
  priority?:number;severity?:string;updated_at?:string;
}
