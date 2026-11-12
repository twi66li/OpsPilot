import { useState } from 'react';
import { App, Button, Popconfirm } from 'antd';
import { post } from './api';

export default function DeleteRecordsButton({ids, disabled, onDeleted, label = '删除选中记录'}: {ids: string[]; disabled?: boolean; onDeleted: () => void | Promise<void>; label?: string}) {
  const {message} = App.useApp(), [busy, setBusy] = useState(false);
  async function remove() {
    setBusy(true);
    try {await post('/api/sessions/delete', {ids}); await onDeleted(); message.success('记录已从工作区删除');}
    catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <Popconfirm title={`删除 ${ids.length} 条记录？`} description="将同时移除相关问题、对话与证据的访问入口；审批及执行审计保留。" okText="确认删除" cancelText="取消" onConfirm={remove} disabled={disabled || !ids.length}>
    <Button danger size="small" loading={busy} disabled={disabled || !ids.length}>{label}</Button>
  </Popconfirm>;
}
