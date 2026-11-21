import { useState } from 'react';
import { CloseOutlined } from '@ant-design/icons';

export default function NoticeBanner({children}: {children: React.ReactNode}) {
  const [closed, setClosed] = useState(false);
  if (closed) return null;
  return (
    <div className="notice-banner auth-topbar-notice" role="status">
      <span className="auth-notice-dot" aria-hidden="true" />
      <span>{children}</span>
      <button type="button" className="notice-close-btn" aria-label="关闭提示" onClick={() => setClosed(true)}>
        <CloseOutlined aria-hidden="true" />
      </button>
    </div>
  );
}
