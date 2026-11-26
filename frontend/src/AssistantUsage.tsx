import { useEffect, useState } from 'react';
import { Modal, Spin } from 'antd';
import { api } from './api';

type Bucket = {used: number; limit: number} | null;
type Usage = {chats: Bucket; tokens: Bucket; uploads: Bucket; resets_at: string};
const number = new Intl.NumberFormat('zh-CN');

export default function AssistantUsage({close}: {close: () => void}) {
  const [data, setData] = useState<Usage | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const request = new AbortController();
    api<Usage>('/api/assistant/usage', {signal: request.signal}).then(setData)
      .catch(e => {if (!request.signal.aborted) setError(e.message);});
    return () => request.abort();
  }, []);

  return <Modal open title="今日用量" onCancel={close} footer={null}>
    {error ? <p role="alert">{error}</p> : !data ? <Spin /> : !data.chats ? <p>管理员无个人日额度。</p>
      : <><dl className="assistant-usage">
        {([['新对话', data.chats], ['平台 token', data.tokens], ['文档入库', data.uploads]] as const)
          .map(([label, bucket]) => bucket && <div key={label}><dt>{label}</dt><dd>{number.format(bucket.used)} / {number.format(bucket.limit)}</dd></div>)}
      </dl><p className="muted">每天北京时间 00:00 恢复额度。</p></>}
  </Modal>;
}
