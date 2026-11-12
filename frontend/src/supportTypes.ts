export const caseStates: Record<string, string> = {investigating: '排查中', needs_information: '待补充信息', awaiting_results: '等待操作反馈', evidence_submitted: '新结果待核对', sample_recovered: '样本显示原异常消失', user_resolved: '用户确认解决'};
export const workflows: {value:Workflow;label:string}[] = [{value: 'auto', label: '自动识别任务'}, {value: 'consult', label: '维护知识咨询'}, {value: 'diagnose', label: '异常排查'}, {value: 'review', label: '操作结果核对'}, {value: 'history', label: '历史案例查询'}];
export const workflowNames: Record<string, string> = Object.fromEntries(workflows.map(w => [w.value, w.label]));
import type { Workflow } from './types';
