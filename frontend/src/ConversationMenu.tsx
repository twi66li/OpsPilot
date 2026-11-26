import { useState } from 'react';
import { App, Button, Dropdown, Input, Modal } from 'antd';
import { MoreOutlined } from '@ant-design/icons';
import { api } from './api';

export default function ConversationMenu({title, path, disabled, renamed, deleted}: {
  title: string; path: string; disabled: boolean; renamed: (title: string) => void; deleted?: () => void;
}) {
  const {modal, message} = App.useApp();
  const [name, setName] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  async function rename() {
    if (!name?.trim() || saving) return;
    setSaving(true);
    try {
      const result = await api<{title: string}>(path, {method: 'PATCH', body: JSON.stringify({title: name.trim()})});
      renamed(result.title);
      setName(null);
    } catch (error) {message.error((error as Error).message);}
    finally {setSaving(false);}
  }
  return <>
    <Dropdown trigger={['click']} menu={{items: [
      {key: 'rename', label: '重命名'},
      ...(deleted ? [{key: 'delete', label: '删除', danger: true}] : []),
    ], onClick: ({key}) => {
      if (key === 'rename') setName(title);
      else if (deleted) modal.confirm({title: '删除这条对话？', okText: '删除', okButtonProps: {danger: true},
        cancelText: '取消', onOk: async () => {
          try {await api(path, {method: 'DELETE'}); deleted();}
          catch (error) {message.error((error as Error).message);}
        }});
    }}}>
      <Button size="small" type="text" aria-label={`${title}的更多操作`} disabled={disabled} icon={<MoreOutlined />} />
    </Dropdown>
    <Modal open={name !== null} title="重命名对话" okText="保存" cancelText="取消" confirmLoading={saving}
      okButtonProps={{disabled: !name?.trim()}} onCancel={() => setName(null)} onOk={rename}>
      <Input aria-label="对话名称" value={name || ''} maxLength={100} onChange={e => setName(e.target.value)} onPressEnter={rename} />
    </Modal>
  </>;
}
