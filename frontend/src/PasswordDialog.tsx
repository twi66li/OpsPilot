import { useState } from 'react';
import { App, Form, Input, Modal } from 'antd';
import { post } from './api';

export default function PasswordDialog({open, close, changed}: {open:boolean; close:()=>void; changed:()=>void}) {
  const [form] = Form.useForm(), [busy,setBusy] = useState(false), {message} = App.useApp();
  async function submit() {
    try {const values = await form.validateFields();setBusy(true);await post('/auth/password',values);message.success('密码已修改，请重新登录');changed();}
    catch(e){if(e instanceof Error)message.error(e.message);}finally{setBusy(false);}
  }
  return <Modal open={open} title="修改密码" onCancel={close} onOk={submit} confirmLoading={busy} okText="修改并重新登录">
    <p>修改后会撤销所有设备的旧登录。</p><Form form={form} layout="vertical">
      <Form.Item name="current_password" label="当前密码" rules={[{required:true}]}><Input.Password autoComplete="current-password" /></Form.Item>
      <Form.Item name="new_password" label="新密码" rules={[{required:true,min:8,max:64,message:'请输入8～64字符的新密码；生产管理员至少12字符'}]}><Input.Password autoComplete="new-password" /></Form.Item>
      <Form.Item name="confirm" label="确认新密码" dependencies={['new_password']} rules={[{required:true},({getFieldValue})=>({validator:(_,value)=>getFieldValue('new_password')===value?Promise.resolve():Promise.reject(new Error('两次密码不一致'))})]}><Input.Password autoComplete="new-password" /></Form.Item>
    </Form>
  </Modal>;
}
