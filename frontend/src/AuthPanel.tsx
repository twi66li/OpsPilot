import { useEffect, useState } from 'react';
import { App, Button, Card, Form, Input, Tabs } from 'antd';
import { api, post, saveAuth } from './api';
import type { Auth } from './types';

export default function AuthPanel({signedIn}: {signedIn: (auth: Auth) => void}) {
  const {message} = App.useApp();
  const [mode, setMode] = useState('login'), [busy, setBusy] = useState(false);
  const registering = mode === 'register';
  const [registration, setRegistration] = useState(false);
  useEffect(()=>{api<{registration_enabled:boolean}>('/auth/options').then(v=>setRegistration(v.registration_enabled)).catch(()=>setRegistration(false));},[]);
  async function submit(values: {username: string; password: string}) {
    setBusy(true);
    try {
      const auth = await post<Auth>(`/auth/${mode}`, {username: values.username, password: values.password});
      saveAuth(auth); signedIn(auth); message.success(registering ? '注册成功，已进入你的私人工作区' : '登录成功');
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="login-page"><section className="login-story"><div className="login-brand"><span className="brand-symbol" aria-hidden="true"/>OpsPilot</div><h1>网站出了问题，<br />陪你一步步排查。</h1><ol className="login-flow"><li>粘贴报错</li><li>查阅官方资料</li><li>补充检查结果</li></ol></section>
    <Card className="login-card" title="欢迎使用 OpsPilot">
      <Tabs activeKey={mode} onChange={setMode} items={[{key:'login',label:'登录'},...(registration?[{key:'register',label:'注册'}]:[])]} />
      <Form key={mode} layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item label="用户名" name="username" rules={[{required:true,message:'请输入用户名'}, ...(registering ? [{min:3,max:64,message:'用户名需要3～64个字符'}] : [])]}><Input autoComplete="username" size="large" maxLength={64} /></Form.Item>
        <Form.Item label="密码" name="password" rules={[{required:true,message:'请输入密码'}, ...(registering ? [{min:6,max:64,message:'密码需要6～64个字符'}] : [])]}><Input.Password autoComplete={registering ? 'new-password' : 'current-password'} size="large" maxLength={64} /></Form.Item>
        {registering ? <Form.Item label="确认密码" name="confirmation" dependencies={['password']} rules={[{required:true,message:'请再次输入密码'}, ({getFieldValue}) => ({validator:(_,value) => !value || getFieldValue('password') === value ? Promise.resolve() : Promise.reject(new Error('两次密码不一致'))})]}><Input.Password autoComplete="new-password" size="large" maxLength={64} /></Form.Item> : <div className="auth-confirmation-space" aria-hidden="true"><Form.Item label="确认密码"><Input.Password disabled tabIndex={-1} size="large" visibilityToggle={false}/></Form.Item></div>}
        <Button type="primary" size="large" htmlType="submit" loading={busy} block>{registering ? '注册' : '登录'}</Button>
      </Form>
      <div className="auth-notice"><p className="muted">{registering ? '普通账号可管理自己的网站、手册、问题和维护对话。每次注册创建独立私人空间，不会加入其他组织。' : !registration ? '当前部署关闭自助注册，请联系部署者获取账号。' : null}</p></div>
    </Card>
  </div>;
}
