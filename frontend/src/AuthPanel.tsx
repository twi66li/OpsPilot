import { useEffect, useState } from 'react';
import { App, Button, Card, Divider, Form, Input, Tabs } from 'antd';
import { api, post, saveAuth } from './api';
import type { Auth } from './types';
import InfoTip from './InfoTip';
import NoticeBanner from './NoticeBanner';
import { BrandMark, ThemeToggleButton } from './themeMode';

export default function AuthPanel({signedIn}: {signedIn: (auth: Auth) => void}) {
  const {message} = App.useApp();
  const [mode, setMode] = useState('login'), [busy, setBusy] = useState(false);
  const registering = mode === 'register';
  const [registration, setRegistration] = useState(false);
  const [visitor,setVisitor]=useState<{username:string;password:string}|null>(null);
  useEffect(()=>{api<{registration_enabled:boolean;visitor?:{username:string;password:string}|null}>('/auth/options').then(v=>{setRegistration(v.registration_enabled);setVisitor(v.visitor||null);}).catch(()=>setRegistration(false));},[]);
  async function submit(values: {username: string; password: string}) {
    setBusy(true);
    try {
      const guest=visitor&&values.username===visitor.username;
      const auth = await post<Auth>(`/auth/${guest?'login':mode}`, {username: values.username, password: values.password});
      saveAuth(auth); signedIn(auth); message.success(auth.is_guest?'已进入只读示例工作区':registering ? '注册成功，已进入你的私人工作区' : '登录成功');
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="auth-screen">
    <div className="auth-bg-canvas" aria-hidden="true">
      <div className="auth-aura auth-aura-1" />
      <div className="auth-aura auth-aura-2" />
      <svg className="auth-bg-svg" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" fill="none">
        <defs>
          <pattern id="authWideGrid" width="96" height="96" patternUnits="userSpaceOnUse">
            <path d="M 96 0 L 0 0 0 96" stroke="var(--grid-stroke)" strokeWidth="1" />
          </pattern>
          <radialGradient id="authGridMaskGrad" cx="55%" cy="48%" r="62%">
            <stop offset="0%" stopColor="#fff" stopOpacity="1" />
            <stop offset="100%" stopColor="#fff" stopOpacity="0" />
          </radialGradient>
          <mask id="authFadeMask">
            <rect width="1440" height="900" fill="url(#authGridMaskGrad)" />
          </mask>
        </defs>
        <rect width="1440" height="900" fill="url(#authWideGrid)" mask="url(#authFadeMask)" />
        <g stroke="var(--arc-stroke)" strokeWidth="1">
          <circle cx="1180" cy="460" r="260" />
          <circle cx="1180" cy="460" r="420" />
          <circle cx="1180" cy="460" r="600" />
          <circle cx="1180" cy="460" r="800" strokeDasharray="6 10" />
        </g>
        <g stroke="var(--arc-stroke)" strokeWidth="1" opacity="0.7">
          <circle cx="180" cy="820" r="320" />
          <circle cx="180" cy="820" r="520" strokeDasharray="4 10" />
        </g>
      </svg>
    </div>

    <div className="auth-topbar">
      <div>{visitor && <NoticeBanner>可先以访客浏览示例网站与排障记录；自己的数据请使用个人账号。</NoticeBanner>}</div>
      <ThemeToggleButton />
    </div>

    <div className="login-page">
      <section className="login-story">
        <div className="login-brand"><BrandMark size={32} />OpsPilot</div>
        <h1>网站出了问题，<br />陪你<span className="headline-accent">一步步排查</span>。</h1>
        <ol className="login-flow">
          <li><span className="step-main">脱敏提交日志或接入告警</span><span className="step-tag">Webhook / HTTP</span></li>
          <li><span className="step-main">混合检索官方资料与手册</span><span className="step-tag">Milvus + BM25</span></li>
          <li><span className="step-main">补充复测样本由程序核对</span><span className="step-tag">只读 Skill</span></li>
          <li><span className="step-main">变更预检与人工审批闭环</span><span className="step-tag">Preflight</span></li>
        </ol>
      </section>

      <div className="login-mobile-brand">
        <div className="login-mobile-brand-mark"><BrandMark size={26} />OpsPilot</div>
      </div>

      <Card className={`login-card ${visitor?'has-visitor':''}`}>
        <div className="auth-tabs">
          <Tabs activeKey={mode} onChange={setMode} items={[{key:'login',label:'登录'},...(registration?[{key:'register',label:'注册'}]:[])]} />
          <InfoTip label={registration?'注册说明':'账号获取说明'}>{registration?'普通账号可管理自己的网站、手册、问题和维护对话。每次注册创建独立私人空间，不会加入其他组织。':'当前部署关闭自助注册，请联系部署者获取账号。'}</InfoTip>
        </div>
        <Form className="auth-body" key={mode} layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item label="用户名" name="username" rules={[{required:true,message:'请输入用户名'}, ...(registering ? [{min:3,max:64,message:'用户名需要3～64个字符'}] : [])]}><Input autoComplete="username" placeholder="请输入用户名" size="large" maxLength={64} /></Form.Item>
          <div className={`auth-password-row ${registering?'registering':''}`}>
            <Form.Item label="密码" name="password" rules={[{required:true,message:'请输入密码'}, ...(registering ? [{min:6,max:64,message:'密码需要6～64个字符'}] : [])]}><Input.Password autoComplete={registering ? 'new-password' : 'current-password'} placeholder="请输入密码" size="large" maxLength={64} /></Form.Item>
            {registering && <Form.Item label="确认密码" name="confirmation" dependencies={['password']} rules={[{required:true,message:'请再次输入密码'}, ({getFieldValue}) => ({validator:(_,value) => !value || getFieldValue('password') === value ? Promise.resolve() : Promise.reject(new Error('两次密码不一致'))})]}><Input.Password autoComplete="new-password" placeholder="请再次输入密码" size="large" maxLength={64} /></Form.Item>}
          </div>
          <Button type="primary" size="large" htmlType="submit" loading={busy} block>{registering ? '注册' : '登录'}</Button>
          {!registering&&visitor&&<section className="visitor-login" aria-label="访客体验账号"><Divider plain>或</Divider><Button className="visitor-enter-btn" block size="large" loading={busy} onClick={()=>void submit(visitor)}>访客直接进入</Button></section>}
        </Form>
      </Card>
    </div>
  </div>;
}
