import { Button, Card, Space, Steps, Tag } from 'antd';

export default function Home({navigate}: {navigate: (page: 'websites' | 'issues' | 'support' | 'knowledge' | 'investigation') => void}) {
  return <div className="page-stack home-page">
    <section className="home-intro"><span className="eyebrow">OPSPILOT / 网站维护助手</span>
      <h1>网站怎么维护，<br />问题如何处理，都有记录。</h1>
      <p>集中查看网站环境、维护手册和历史问题。需要帮助时，向助手咨询，或提交日志逐步排查。<br />无需接入监控，网站背景和手册也可以稍后补充。</p>
      <Space wrap><Button type="primary" size="large" onClick={() => navigate('websites')}>管理我的网站</Button><Button size="large" onClick={() => navigate('support')}>直接问维护助手</Button></Space>
    </section>
    <div className="home-cards">
      <Card title="网站遇到问题"><Tag>问题中心</Tag><p>记录异常、影响与近期变更，查看检查进度和处理结果。排查中的问题可随时继续，解决和归档分别管理。</p><Button onClick={() => navigate('issues')}>查看与处理问题</Button></Card>
      <Card title="想了解网站怎么维护"><Tag>资料与咨询</Tag><p>内置 Python、Docker、Nginx、PostgreSQL 排障资料。选填网站背景或上传自己的部署手册，让回答更贴合环境。</p><Button onClick={() => navigate('knowledge')}>查看资料库</Button></Card>
      <Card title="先体验一次故障处理"><Tag color="gold">独立测试环境</Tag><p>了解连接池超时会产生哪些异常，再体验证据调查、人工审批和恢复请求验证。演练不会操作你的网站。</p><Button onClick={() => navigate('investigation')}>了解并进入演练</Button></Card>
    </div>
    <Card title="第一次使用，按这三步就可以">
      <Steps items={[{title: '选择网站或直接提问', content: '记录环境与资料，也可以先做基础咨询。'}, {title: '咨询或逐步排查', content: '知识问答直接解释；故障问题记录证据、方案和逐项检查结果。'}, {title: '确认实际结果', content: '程序核对提交样本，用户确认问题结局，再决定是否归档。'}]} />
      <p className="muted">支持先从 Python 网站的数据库连接失败、连接池超时、反向代理 502 / 504、容器异常开始。助手提供指导，个人网站的操作由你执行。</p>
    </Card>
  </div>;
}
