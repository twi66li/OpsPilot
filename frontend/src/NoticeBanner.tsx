import { Alert } from 'antd';

export default function NoticeBanner({children}: {children: React.ReactNode}) {
  return <Alert className="notice-banner" type="info" title={children} closable/>;
}
