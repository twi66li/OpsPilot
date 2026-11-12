import { useEffect, useMemo, useState } from 'react';
import { Alert, App as AntApp, Button, Drawer, Grid, Layout, Menu, Space, Tag } from 'antd';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, post, savedAuth, saveAuth } from './api';
import type { Auth, Hit, SupportEntry } from './types';
import Investigation from './Investigation';
import Operations from './Operations';
import Evaluation from './Evaluation';
import SourceDrawer from './SourceDrawer';
import Home from './Home';
import OfficialLibrary from './OfficialLibrary';
import PersonalSupport from './PersonalSupport';
import Websites from './Websites';
import Issues from './Issues';
import AuthPanel from './AuthPanel';
import PasswordDialog from './PasswordDialog';
import TodoCenter from './TodoCenter';

const pages = {home:'开始使用',websites:'我的网站',issues:'问题中心',support:'智能维护助手',knowledge:'维护资料库',investigation:'故障演练',operations:'演练审批与工单',evaluation:'评测记录'};
type Page = keyof typeof pages;
export default function App() {
  const {message} = AntApp.useApp(), navigate = useNavigate(), location = useLocation(), screens = Grid.useBreakpoint();
  const [auth,setAuth] = useState<Auth|null>(savedAuth), [source,setSource] = useState<Hit|null>(null);
  const [healthy,setHealthy] = useState<boolean|null>(null), [drawer,setDrawer] = useState(false), [passwordOpen,setPasswordOpen] = useState(false);
  const parts = location.pathname.split('/').filter(Boolean), page:Page = parts[0] in pages ? parts[0] as Page : 'home', resourceId = parts[1];
  const supportEntry = useMemo<SupportEntry>(()=>{
    const params = new URLSearchParams(location.search);
    return {caseId:resourceId,siteId:params.get('site') || undefined,workflow:(params.get('workflow') || 'auto') as SupportEntry['workflow'],question:params.get('question') || undefined};
  },[resourceId,location.search]);
  const canWrite = !!auth && ['admin','engineer'].includes(auth.role);
  function setPage(target:Page) {navigate('/'+target);setDrawer(false);}
  function assist(entry:SupportEntry) {
    const params = new URLSearchParams();if(entry.siteId)params.set('site',entry.siteId);if(entry.workflow)params.set('workflow',entry.workflow);if(entry.question)params.set('question',entry.question);
    navigate('/support'+(entry.caseId?'/'+entry.caseId:'')+(params.size?'?'+params:''));
  }
  function clearAuth() {saveAuth(null);setAuth(null);setSource(null);setPasswordOpen(false);}
  async function logout() {try{await post('/auth/logout');clearAuth();}catch(e){message.error((e as Error).message);}}
  useEffect(()=>{
    const unauthorized=()=>{saveAuth(null);setAuth(null);setSource(null);};window.addEventListener('opspilot:unauthorized',unauthorized);
    api('/ready').then(()=>setHealthy(true)).catch(()=>setHealthy(false));if(savedAuth())api('/auth/me').catch(()=>unauthorized());
    return ()=>window.removeEventListener('opspilot:unauthorized',unauthorized);
  },[]);
  if(!auth)return <AuthPanel signedIn={setAuth}/>;
  const navigation=<><div className="brand"><span className="brand-symbol">O</span><span>OpsPilot</span></div><div className="sidebar-label">个人网站维护助手</div>
    <Menu theme="dark" mode="inline" selectedKeys={[page]} onClick={({key})=>setPage(key as Page)} items={Object.entries(pages).filter(([key])=>(key!=='evaluation'||auth.role==='admin')&&(key!=='operations'||canWrite)).map(([key,label])=>({key,label}))}/></>;
  return <Layout className="app-layout">
    {screens.lg&&<Layout.Sider width={204} className="main-sidebar">{navigation}<div className="sidebar-foot">{healthy===false?'服务依赖异常':healthy?'服务已就绪':'正在连接'}</div></Layout.Sider>}
    <Drawer open={drawer} onClose={()=>setDrawer(false)} placement="left" title="功能导航" size={260} styles={{body:{padding:0,background:'#18343f'}}}>{navigation}</Drawer>
    <Layout><Layout.Header className="app-header"><Space>{!screens.lg&&<Button aria-label="打开功能导航" onClick={()=>setDrawer(true)}>☰ 菜单</Button>}<strong>{pages[page]}</strong></Space>
      <Space wrap size="small"><TodoCenter/><Tag>{{admin:'管理员',engineer:'工程师',viewer:'普通用户'}[auth.role]||auth.role}</Tag><span className="account-name">{auth.username}</span><Button size="small" onClick={()=>setPasswordOpen(true)}>改密</Button><Button size="small" onClick={logout}>退出</Button></Space>
    </Layout.Header><Layout.Content className={`app-content ${page==='investigation'?'chat-content':''}`}>
      {page==='home'&&<Home navigate={setPage}/>}
      {page==='websites'&&<Websites assist={assist} knowledge={()=>setPage('knowledge')} initialId={resourceId} opened={id=>navigate('/websites/'+id)}/>}
      {page==='issues'&&(resourceId?<PersonalSupport entry={supportEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')} onIssues={()=>setPage('issues')} opened={id=>navigate('/issues/'+id)}/>:<Issues assist={entry=>navigate('/issues/'+entry.caseId)}/>)}
      {page==='support'&&<PersonalSupport entry={supportEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')} onIssues={()=>setPage('issues')} opened={id=>navigate('/support/'+id)}/>}
      {page==='investigation'&&<Investigation onSource={setSource} canApprove={canWrite} initialId={resourceId} opened={id=>navigate('/investigation/'+id)}/>}
      {page==='knowledge'&&<OfficialLibrary onSource={setSource}/>}
      {page==='operations'&&(canWrite?<Operations canApprove={canWrite}/>:<Alert type="warning" title="此模块需要工程师或管理员权限"/>)}
      {page==='evaluation'&&(auth.role==='admin'?<Evaluation/>:<Alert type="warning" title="评测记录仅管理员可查看"/>)}
    </Layout.Content></Layout><SourceDrawer hit={source} close={()=>setSource(null)}/><PasswordDialog open={passwordOpen} close={()=>setPasswordOpen(false)} changed={clearAuth}/>
  </Layout>;
}
