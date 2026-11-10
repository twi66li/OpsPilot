import { useEffect, useState } from 'react';
import { App as AntApp, Button, Card, Form, Input, Layout, Menu, Space, Tag } from 'antd';
import { api, post, savedAuth, saveAuth } from './api';
import type { Auth, Hit } from './types';
import Investigation from './Investigation';
import Knowledge from './Knowledge';
import Operations from './Operations';
import Evaluation from './Evaluation';
import SourceDrawer from './SourceDrawer';

const pages = {investigation: '调查工作台', knowledge: '运维知识库', operations: '审批与工单', evaluation: '评测记录'};
type Page = keyof typeof pages;
export default function App() {
  const {message} = AntApp.useApp();
  const [auth, setAuth] = useState<Auth | null>(savedAuth), [loggingIn, setLoggingIn] = useState(false);
  const [page, setPage] = useState<Page>('investigation'), [source, setSource] = useState<Hit | null>(null);
  const [healthy, setHealthy] = useState<boolean | null>(null);
  const canWrite = !!auth && ['admin', 'engineer'].includes(auth.role);
  function logout() {saveAuth(null); setAuth(null); setSource(null); setPage('investigation');}
  useEffect(() => {
    const unauthorized = () => {saveAuth(null); setAuth(null); setSource(null);};
    window.addEventListener('opspilot:unauthorized', unauthorized);
    api('/health').then(() => setHealthy(true)).catch(() => setHealthy(false));
    if (savedAuth()) api('/auth/me').catch(() => unauthorized());
    return () => window.removeEventListener('opspilot:unauthorized', unauthorized);
  }, []);
  async function login(values: {username: string; password: string}) {
    setLoggingIn(true);
    try {const result = await post<Auth>('/auth/login', values); saveAuth(result); setAuth(result); message.success('登录成功');}
    catch (error) {message.error((error as Error).message);} finally {setLoggingIn(false);}
  }
  if (!auth) return <div className="login-page"><div className="login-story"><span className="eyebrow">OPSPILOT / 运维调查</span><h1>让每个判断<br />都有证据可查。</h1><p>从异常描述到调查结论，再到人工审批，<br />在一个工作台里完成。</p></div>
    <Card className="login-card" title="登录工作台"><Form layout="vertical" onFinish={login} requiredMark={false}>
      <Form.Item label="用户名" name="username" rules={[{required: true, message: '请输入用户名'}]}><Input autoComplete="username" size="large" /></Form.Item>
      <Form.Item label="密码" name="password" rules={[{required: true, message: '请输入密码'}]}><Input.Password autoComplete="current-password" size="large" /></Form.Item>
      <Button type="primary" size="large" htmlType="submit" loading={loggingIn} block>登录</Button>
    </Form><p className="muted">使用组织分配的账号访问。数据与审批权限由当前账号决定。</p></Card>
  </div>;
  return <Layout className="app-layout"><Layout.Sider width={204} breakpoint="lg" collapsedWidth={60} className="main-sidebar">
    <div className="brand"><span className="brand-symbol">O</span><span>OpsPilot</span></div><div className="sidebar-label">运维调查工作台</div>
    <Menu theme="dark" mode="inline" selectedKeys={[page]} onClick={({key}) => setPage(key as Page)} items={Object.entries(pages).filter(([key]) => key !== 'evaluation' || auth.role === 'admin').map(([key, label], i) => ({key, label, icon: <span className="nav-number">0{i + 1}</span>}))} />
    <div className="sidebar-foot"><span className="status-dot" />{healthy === false ? '连接异常' : healthy === true ? '服务已连接' : '正在连接'}<br /><small>调查 · 证据 · 审批</small></div>
  </Layout.Sider><Layout><Layout.Header className="app-header"><div><strong>{pages[page]}</strong><span className="header-subtitle">基于证据，审慎执行</span></div><Space><Tag>{auth.role}</Tag><span>{auth.username}</span><Button type="text" onClick={logout}>退出</Button></Space></Layout.Header>
    <Layout.Content className={`app-content ${page === 'investigation' ? 'chat-content' : ''}`}>
      {page === 'investigation' && <Investigation onSource={setSource} canApprove={canWrite} />}
      {page === 'knowledge' && <Knowledge onSource={setSource} canWrite={canWrite} />}
      {page === 'operations' && <Operations canApprove={canWrite} />}
      {page === 'evaluation' && auth.role === 'admin' && <Evaluation />}
    </Layout.Content>
  </Layout><SourceDrawer hit={source} close={() => setSource(null)} /></Layout>;
}
