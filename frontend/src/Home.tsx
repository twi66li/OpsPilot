import { Button, Card, Space, Steps, Tag } from 'antd';

export default function Home({navigate}: {navigate: (page: 'support' | 'knowledge' | 'investigation') => void}) {
  return <div className="page-stack home-page">
    <section className="home-intro"><span className="eyebrow">OPSPILOT / 网站维护助手</span>
      <h1>网站报错了，<br />下一步该检查什么？</h1>
      <p>粘贴报错或日志，结合官方资料与自己的网站背景，逐步排查并核对处理结果。<br />无需接入监控平台，也可以不上传手册。</p>
      <Space wrap><Button type="primary" size="large" onClick={() => navigate('support')}>开始排查我的问题</Button><Button size="large" onClick={() => navigate('knowledge')}>浏览内置排障资料</Button></Space>
    </section>
    <div className="home-cards">
      <Card title="遇到报错，不知道原因"><Tag>个人网站排障</Tag><p>提交异常描述或脱敏日志，得到有依据的可能原因、1–3 条检查步骤和需要补充的信息。</p><Button onClick={() => navigate('support')}>我有一个问题</Button></Card>
      <Card title="想了解网站怎么维护"><Tag>资料与咨询</Tag><p>内置 Python、Docker、Nginx、PostgreSQL 排障资料。选填网站背景或上传自己的部署手册，让回答更贴合环境。</p><Button onClick={() => navigate('knowledge')}>查看资料库</Button></Card>
      <Card title="先体验一次故障处理"><Tag color="gold">独立测试环境</Tag><p>了解连接池超时会产生哪些异常，再体验证据调查、人工审批和恢复请求验证。演练不会操作你的网站。</p><Button onClick={() => navigate('investigation')}>了解并进入演练</Button></Card>
    </div>
    <Card title="第一次使用，按这三步就可以">
      <Steps items={[{title: '描述问题', content: '可选填网站背景；日志先预览脱敏，再确认提交。'}, {title: '逐步检查', content: '根据步骤在自己的环境操作，把结果带回同一会话。'}, {title: '核对与保存', content: '依据新证据判断是否改善，确认后保存处理案例。'}]} />
      <p className="muted">支持先从 Python 网站的数据库连接失败、连接池超时、反向代理 502 / 504、容器异常开始。助手提供指导，个人网站的操作由你执行。</p>
    </Card>
  </div>;
}
