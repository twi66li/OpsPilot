import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { Alert, App as AntApp, Button, Drawer, Dropdown, Grid, Layout, Menu, Space, Spin, Tag } from 'antd';
import { CommentOutlined, LockOutlined, LogoutOutlined, MenuOutlined, UserOutlined } from '@ant-design/icons';
import { useLocation, useNavigate } from 'react-router-dom';
import { api, post, savedAuth, saveAuth } from './api';
import type { Auth, Hit, SupportEntry } from './types';
import SourceDrawer from './SourceDrawer';
import Home from './Home';
import AuthPanel from './AuthPanel';
import PasswordDialog from './PasswordDialog';
import TodoCenter from './TodoCenter';

const Investigation = lazy(() => import('./Investigation'));
const Operations = lazy(() => import('./Operations'));
const Evaluation = lazy(() => import('./Evaluation'));
const OfficialLibrary = lazy(() => import('./OfficialLibrary'));
const PersonalSupport = lazy(() => import('./PersonalSupport'));
const Websites = lazy(() => import('./Websites'));
const Issues = lazy(() => import('./Issues'));

const pages = {home:'维护工作台',websites:'我的网站',issues:'问题中心',support:'智能维护助手',knowledge:'维护资料库',investigation:'故障演练',operations:'审批与工单',evaluation:'评测记录'};
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
  const allowed = (key:Page) => (key !== 'evaluation' || auth?.role === 'admin') && (key !== 'operations' || canWrite);
  const group = (key:string,label:string,keys:Page[]) => ({key,type:'group' as const,className:`nav-group nav-${key}`,label:<span className="nav-group-label">{label}</span>,children:keys.filter(allowed).map(key=>({key,label:pages[key]}))});
  const menuItems = [group('overview','概览',['home']),group('maintenance','网站维护',['websites','issues']),group('resources','资料与演练',['knowledge','investigation']),group('management','管理与质量',['operations','evaluation'])].filter(item=>item.children.length);
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
  const navigation=<><div className="brand"><span className="brand-symbol" aria-hidden="true"/><span>OpsPilot</span></div>
    <Button type="primary" aria-label="智能维护助手" className={`assistant-entry ${page==='support'?'selected':''}`} aria-current={page==='support'?'page':undefined} icon={<CommentOutlined aria-hidden="true"/>} onClick={()=>setPage('support')}>智能维护助手</Button>
    <Menu mode="inline" selectedKeys={[page]} onClick={({key})=>setPage(key as Page)} items={menuItems}/></>;
  return <Layout className="app-layout">
    {screens.lg&&<Layout.Sider width={216} className="main-sidebar">{navigation}<div className={`sidebar-foot ${healthy===false?'unhealthy':''}`}><span className="status-dot" />{healthy===false?'服务依赖异常':healthy?'服务已就绪':'正在连接服务'}</div></Layout.Sider>}
    <Drawer className="navigation-drawer" open={drawer} onClose={()=>setDrawer(false)} placement="left" title="功能导航" size={280} styles={{body:{padding:0,background:'#ffffff'}}}>{navigation}</Drawer>
    <Layout><Layout.Header className="app-header"><Space size="middle">{!screens.lg&&<Button type="text" className="navigation-trigger" aria-label="打开功能导航" icon={<MenuOutlined />} onClick={()=>setDrawer(true)} />}<strong className="header-page-title">{pages[page]}</strong></Space>
      <Space className="app-header-actions" size="small"><TodoCenter/><Tag className="role-tag">{{admin:'管理员',engineer:'工程师',viewer:'普通用户'}[auth.role]||auth.role}</Tag><Dropdown menu={{items:[{key:'password',icon:<LockOutlined/>,label:'修改密码'},{type:'divider'},{key:'logout',icon:<LogoutOutlined/>,label:'退出登录'}],onClick:({key})=>key==='logout'?void logout():setPasswordOpen(true)}}><Button type="text" className="account-button" icon={<UserOutlined />}><span className="account-name">{auth.username}</span></Button></Dropdown></Space>
    </Layout.Header><Layout.Content className={`app-content ${page==='investigation'?'chat-content':''} ${page==='support'||(page==='issues'&&resourceId)?'support-content':''}`}>
      <Suspense fallback={<div className="page-loading"><Spin tip="正在加载工作区" /></div>}>
        {page==='home'&&<Home navigate={path=>navigate(path)} canWrite={canWrite}/>}
        {page==='websites'&&<Websites assist={assist} knowledge={()=>setPage('knowledge')} initialId={resourceId} opened={id=>navigate('/websites/'+id)}/>}
        {page==='issues'&&(resourceId?<PersonalSupport entry={supportEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')} onIssues={()=>setPage('issues')} opened={id=>navigate('/issues/'+id)}/>:<Issues assist={entry=>navigate('/issues/'+entry.caseId)}/>)}
        {page==='support'&&<PersonalSupport entry={supportEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')} onIssues={()=>setPage('issues')} opened={id=>navigate('/support/'+id)}/>}
        {page==='investigation'&&<Investigation onSource={setSource} canApprove={canWrite} initialId={resourceId} opened={id=>navigate('/investigation/'+id)}/>}
        {page==='knowledge'&&<OfficialLibrary onSource={setSource}/>}
        {page==='operations'&&(canWrite?<Operations canApprove={canWrite}/>:<Alert type="warning" title="此模块需要工程师或管理员权限"/>)}
        {page==='evaluation'&&(auth.role==='admin'?<Evaluation/>:<Alert type="warning" title="评测记录仅管理员可查看"/>)}
      </Suspense>
    </Layout.Content></Layout><SourceDrawer hit={source} close={()=>setSource(null)}/><PasswordDialog open={passwordOpen} close={()=>setPasswordOpen(false)} changed={clearAuth}/>
  </Layout>;
}
