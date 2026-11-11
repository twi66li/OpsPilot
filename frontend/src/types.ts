export interface Auth { access_token: string; username: string; role: string; tenant_id: string }
export interface Session { id: string; title: string; created_at: string }
export interface Hit {
  chunk_id: string; doc_id: string; doc_title: string; text: string; source: string;
  version_id?: string; version?: number; page_start?: number; page_end?: number; heading_path?: string[]; citation_id?: number;
  parent_text?: string; file_url?: string; content_type?: string; matched_text?: string; source_name?: string;
}
export interface ChatMessage { role: string; content: string; meta?: { run_id?: string; citations?: string[] } }
export interface Approval { pending?: boolean; approval_id: string; ticket_draft: Record<string, unknown> }
export interface AgentEvent {
  type: string; answer?: string; delta?: string; message?: string; node?: string; name?: string; agent?: string;
  summary?: string; run_id?: string; hits?: Hit[]; approval?: Approval; approval_id?: string;
  ticket_draft?: Record<string, unknown>; result?: Record<string, unknown>; end_reason?: string;
}
export interface Run {
  run_id: string; status: string;
  data: { result?: AgentEvent; events: { kind: string; detail: AgentEvent | Record<string, unknown> }[] };
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
