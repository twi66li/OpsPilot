import { useState } from 'react';
import { Alert, App, Button, Card, Form, Input, Tabs } from 'antd';
import { post, saveAuth } from './api';
import type { Auth } from './types';

export default function AuthPanel({signedIn}: {signedIn: (auth: Auth) => void}) {
  const {message} = App.useApp();
  const [mode, setMode] = useState('login'), [busy, setBusy] = useState(false);
  const registering = mode === 'register';
  async function submit(values: {username: string; password: string}) {
    setBusy(true);
    try {
      const auth = await post<Auth>(`/auth/${mode}`, {username: values.username, password: values.password});
      saveAuth(auth); signedIn(auth); message.success(registering ? '注册成功，已进入你的私人工作区' : '登录成功');
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  }
  return <div className="login-page"><div className="login-story"><span className="eyebrow">OPSPILOT / 网站维护助手</span><h1>网站出了问题，<br />陪你一步步排查。</h1><p>粘贴报错、查阅官方资料、补充检查结果。<br />无需接入监控，也可以从一个问题开始。</p></div>
    <Card className="login-card" title="欢迎使用 OpsPilot">
      <Tabs activeKey={mode} onChange={setMode} items={[{key:'login',label:'登录'},{key:'register',label:'注册普通账号'}]} />
      <Form key={mode} layout="vertical" onFinish={submit} requiredMark={false}>
        <Form.Item label="用户名" name="username" rules={[{required:true,message:'请输入用户名'}, ...(registering ? [{min:3,max:64,message:'用户名需要3～64个字符'}] : [])]}><Input autoComplete="username" size="large" maxLength={64} /></Form.Item>
        <Form.Item label="密码" name="password" rules={[{required:true,message:'请输入密码'}, ...(registering ? [{min:6,max:64,message:'密码需要6～64个字符'}] : [])]}><Input.Password autoComplete={registering ? 'new-password' : 'current-password'} size="large" maxLength={64} /></Form.Item>
        {registering && <Form.Item label="确认密码" name="confirmation" dependencies={['password']} rules={[{required:true,message:'请再次输入密码'}, ({getFieldValue}) => ({validator:(_,value) => !value || getFieldValue('password') === value ? Promise.resolve() : Promise.reject(new Error('两次密码不一致'))})]}><Input.Password autoComplete="new-password" size="large" maxLength={64} /></Form.Item>}
        <Button type="primary" size="large" htmlType="submit" loading={busy} block>{registering ? '注册并进入工作台' : '登录'}</Button>
      </Form>
      <p className="muted">{registering ? '普通账号可管理自己的网站、手册、问题和维护对话。每次注册创建独立私人空间，不会加入其他组织。' : '首次使用可直接注册普通账号，无需组织分配。'}</p>
      <Alert type="info" title="账号权限" description="普通用户使用个人维护功能；工程师处理演练审批；管理员另可管理组织账号和查看评测。管理员由部署者配置，不开放自助注册。" />
      {['localhost','127.0.0.1','[::1]'].includes(window.location.hostname) && <p className="muted">本机演示默认管理员：admin / admin123。若部署者已修改，请使用配置后的账号。</p>}
    </Card>
  </div>;
}
