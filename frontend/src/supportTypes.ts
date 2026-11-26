import type { Workflow } from './types';

export const caseStates: Record<string, string> = {
  investigating: '诊断中',
  needs_information: '需要更多信息',
  awaiting_results: '待验证',
  evidence_submitted: '待分析',
  sample_recovered: '看起来已恢复',
  user_resolved: '已解决',
};
export const verdictLabels: Record<string, string> = {
  inconclusive: '还需要更多信息',
  still_failing: '异常仍在出现',
  improved: '情况有所好转',
  sample_recovered: '看起来已恢复',
};
export const workflows: {value: Workflow; label: string}[] = [
  {value: 'auto', label: '自动识别任务'},
  {value: 'consult', label: '维护知识咨询'},
  {value: 'diagnose', label: '异常排查'},
  {value: 'review', label: '操作结果核对'},
  {value: 'history', label: '历史案例查询'},
];
export const workflowNames: Record<string, string> = Object.fromEntries(workflows.map(w => [w.value, w.label]));
