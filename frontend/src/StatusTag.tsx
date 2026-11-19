import { Tag } from 'antd';
import { caseStates } from './supportTypes';

const colors:Record<string,string>={investigating:'processing',needs_information:'warning',awaiting_results:'warning',evidence_submitted:'processing',sample_recovered:'warning',user_resolved:'success',failed:'error',interrupted:'error',running:'processing',cancel_requested:'warning',cancelled:'default',completed:'success',pending:'warning'};
const labels:Record<string,string>={...caseStates,failed:'执行失败',interrupted:'执行被中断',running:'正在调查',cancel_requested:'正在停止',cancelled:'已停止',completed:'调查完成',pending:'等待审批'};
export default function StatusTag({status}:{status:string}){return <Tag color={colors[status]}>{labels[status]||status}</Tag>;}
export function SeverityTag({value}:{value?:string}){return <Tag color={value==='P0'||value==='P1'?'error':value==='P2'?'warning':undefined}>{value||'未设置'}</Tag>;}
