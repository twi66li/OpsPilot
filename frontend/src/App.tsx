import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, App as AntApp, Button, Drawer, Dropdown, Layout, Menu, Space, Spin } from 'antd';
import { BookOutlined, CommentOutlined, DashboardOutlined, ExperimentOutlined, FileSearchOutlined, GlobalOutlined, LockOutlined, LogoutOutlined, MenuFoldOutlined, MenuOutlined, MenuUnfoldOutlined, RobotOutlined, SearchOutlined, SafetyCertificateOutlined, UnorderedListOutlined, UserOutlined } from '@ant-design/icons';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { api, post, savedAuth, saveAuth } from './api';
import type { Auth, Hit, SupportEntry } from './types';
import SourceDrawer from './SourceDrawer';
import Home from './Home';
import AuthPanel from './AuthPanel';
import PasswordDialog from './PasswordDialog';
import AssistantSettings, { type PersonalModel } from './AssistantSettings';
import AssistantUsage from './AssistantUsage';
import TodoCenter from './TodoCenter';
import Capabilities from './Capabilities';
import CommandPalette from './CommandPalette';
import ErrorBoundary from './ErrorBoundary';
import { BrandMark, ThemeToggleButton } from './themeMode';

const Investigation = lazy(() => import('./Investigation'));
const Operations = lazy(() => import('./Operations'));
const Evaluation = lazy(() => import('./Evaluation'));
const OfficialLibrary = lazy(() => import('./OfficialLibrary'));
const PersonalSupport = lazy(() => import('./PersonalSupport'));
const AssistantChat = lazy(() => import('./AssistantChat'));
const Websites = lazy(() => import('./Websites'));
const Issues = lazy(() => import('./Issues'));

const pages = {home:'维护工作台',websites:'我的网站',issues:'问题中心',support:'智能维护助手',knowledge:'维护资料库',investigation:'故障演练',operations:'审批与工单',evaluation:'评测记录'};
type Page = keyof typeof pages;

export default function App() {
  const {message} = AntApp.useApp(), navigate = useNavigate(), location = useLocation();
  const [auth,setAuth] = useState<Auth|null>(savedAuth), [source,setSource] = useState<Hit|null>(null);
  const [healthy,setHealthy] = useState<boolean|null>(null), [drawer,setDrawer] = useState(false), [passwordOpen,setPasswordOpen] = useState(false);
  const [sideEntry,setSideEntry]=useState<SupportEntry|null>(null),[capabilitiesOpen,setCapabilitiesOpen]=useState(false);
  const [model, setModel] = useState<PersonalModel | null>(null);
  const [modelOpen, setModelOpen] = useState(false), [usageOpen, setUsageOpen] = useState(false);
  const [drillEnabled,setDrillEnabled]=useState<boolean|null>(null);
  useEffect(()=>{
    const controller=new AbortController();
    api<{drill_enabled:boolean}>('/auth/options',{signal:controller.signal}).then(
      value=>setDrillEnabled(value.drill_enabled===true),
      ()=>{if(!controller.signal.aborted)setDrillEnabled(false);},
    );
    return ()=>controller.abort();
  },[]);
  const [searchOpen,setSearchOpen]=useState(false),[windowWidth,setWindowWidth]=useState(window.innerWidth),[resourceTitle,setResourceTitle]=useState<{id:string;name:string}|null>(null);
  const siteTitle=useCallback((id:string,name:string)=>setResourceTitle({id,name}),[]);
  const [navCollapsed,setNavCollapsed]=useState(()=>{try{return localStorage.getItem('opspilot.nav-collapsed')==='true';}catch{return false;}});
  const mobile=windowWidth<=700,collapsed=!mobile&&(windowWidth<=1000||navCollapsed);
  const parts = location.pathname.split('/').filter(Boolean), page:Page = parts[0] in pages ? parts[0] as Page : 'home', resourceId = parts[1];
  const plainChat = page === 'support' && (resourceId === 'chat' || (!resourceId && !location.search));
  const fromChatId = (location.state as {fromChatId?: string} | null)?.fromChatId;
  const supportEntry = useMemo<SupportEntry>(()=>{
    const params = new URLSearchParams(location.search);
    return {caseId:resourceId,siteId:params.get('site') || undefined,workflow:(params.get('workflow') || 'auto') as SupportEntry['workflow'],question:params.get('question') || undefined};
  },[resourceId,location.search]);
  const canWrite = !!auth && ['admin','engineer'].includes(auth.role);
  const canReadEvaluation = auth?.role === 'admin' && auth.tenant_id === 'default';
  useEffect(()=>{const resize=()=>setWindowWidth(window.innerWidth);window.addEventListener('resize',resize);return()=>window.removeEventListener('resize',resize);},[]);
  useEffect(()=>{try{localStorage.setItem('opspilot.nav-collapsed',String(navCollapsed));}catch{/* 浏览器禁用存储时仍可在本次使用中折叠。 */}},[navCollapsed]);
  useEffect(()=>{if(mobile)setDrawer(false);},[mobile]);
  useEffect(()=>{const key=(event:KeyboardEvent)=>{if(auth&&(event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();setSearchOpen(value=>!value);}};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key);},[auth]);
  const allowed = (key:Page) => (key !== 'evaluation' || canReadEvaluation) && (key !== 'operations' || canWrite) && (key !== 'investigation' || drillEnabled === true);
  const icons={home:<DashboardOutlined/>,websites:<GlobalOutlined/>,issues:<UnorderedListOutlined/>,support:<CommentOutlined/>,knowledge:<BookOutlined/>,investigation:<ExperimentOutlined/>,operations:<SafetyCertificateOutlined/>,evaluation:<FileSearchOutlined/>};
  const group = (key:string,label:string,keys:Page[]) => ({key,type:'group' as const,className:`nav-group nav-${key}`,label:<span className="nav-group-label">{label}</span>,children:keys.filter(key=>allowed(key)||(auth?.is_guest&&key!=='investigation')).map(key=>({key,label:pages[key],icon:allowed(key)?icons[key]:<LockOutlined/>}))});
  const menuItems = [group('overview','概览',['home']),group('maintenance','网站维护',['websites','issues']),group('resources',drillEnabled?'资料与演练':'资料',['knowledge','investigation']),group('management','管理与质量',['operations','evaluation'])].filter(item=>item.children.length);
  function setPage(target:Page) {navigate('/'+target);setDrawer(false);}
  function assist(entry:SupportEntry) {
    const params = new URLSearchParams({mode:'diagnose'});if(entry.siteId)params.set('site',entry.siteId);if(entry.workflow)params.set('workflow',entry.workflow);if(entry.question)params.set('question',entry.question);
    navigate('/support'+(entry.caseId?'/'+entry.caseId:'')+(params.size?'?'+params:''));
  }
  function askHere(entry?:SupportEntry){setSideEntry(entry||{...(page==='websites'&&resourceId?{siteId:resourceId}:page==='issues'&&resourceId?{caseId:resourceId}:{}),workflow:'consult'});}
  useEffect(()=>{setSideEntry(null);},[location.pathname,location.search]);
  function clearAuth() {saveAuth(null);setAuth(null);setSource(null);setPasswordOpen(false);setSideEntry(null);setCapabilitiesOpen(false);setSearchOpen(false);setResourceTitle(null);setModel(null);setModelOpen(false);setUsageOpen(false);}
  async function logout() {try{await post('/auth/logout');clearAuth();}catch(e){message.error((e as Error).message);}}
  useEffect(()=>{
    const unauthorized=clearAuth;window.addEventListener('opspilot:unauthorized',unauthorized);
    let readinessCheck: AbortController | undefined;
    const checkReady=()=>{
      readinessCheck?.abort();
      readinessCheck=new AbortController();
      const {signal}=readinessCheck;
      void api('/ready',{signal}).then(
        ()=>{if(!signal.aborted)setHealthy(true);},
        ()=>{if(!signal.aborted)setHealthy(false);},
      );
    };
    checkReady();window.addEventListener('focus',checkReady);if(savedAuth())api('/auth/me').catch(()=>unauthorized());
    return ()=>{readinessCheck?.abort();window.removeEventListener('opspilot:unauthorized',unauthorized);window.removeEventListener('focus',checkReady);};
  },[]);
  if(!auth)return <AuthPanel signedIn={setAuth}/>;
  const roleLabel=auth.is_guest?'访客':{admin:'管理员',engineer:'普通用户',viewer:'普通用户'}[auth.role]||auth.role;
  const navigation=<><div className="brand" title="OpsPilot"><BrandMark size={26}/><span>OpsPilot</span></div>
    <Menu mode="inline" inlineCollapsed={collapsed} selectedKeys={page==='support'?[]:[page]} onClick={({key})=>setPage(key as Page)} items={menuItems}/></>;
  return <Layout className="app-layout">
    {!mobile&&<Layout.Sider width={216} collapsedWidth={64} collapsed={collapsed} className="main-sidebar">{navigation}<div className="sidebar-controls"><button className={`capability-trigger ${healthy===false?'unhealthy':''}`} onClick={()=>setCapabilitiesOpen(true)} aria-label="查看能力状态" title="查看能力与连接状态"><span className="status-dot"/><span>{healthy===false?'服务依赖异常':healthy?'服务已就绪':'正在连接服务'}</span></button><Button type="text" aria-label={collapsed?'展开导航':'折叠导航'} title={collapsed?'展开导航':'折叠导航'} icon={collapsed?<MenuUnfoldOutlined/>:<MenuFoldOutlined/>} disabled={windowWidth<=1000} onClick={()=>setNavCollapsed(value=>!value)}/></div></Layout.Sider>}
    <Drawer className="navigation-drawer" open={drawer} onClose={()=>setDrawer(false)} placement="left" title="功能导航" size={280} styles={{body:{padding:0,background:'var(--panel)'}}}>{navigation}</Drawer>
    <Layout><Layout.Header className="app-header"><Space size="middle">{mobile&&<Button type="text" className="navigation-trigger" aria-label="打开功能导航" icon={<MenuOutlined />} onClick={()=>setDrawer(true)} />}{page==='websites'&&resourceId?<nav className="page-breadcrumb" aria-label="面包屑"><Link to="/websites">我的网站</Link><span aria-hidden="true">/</span><strong aria-current="page">{resourceTitle?.id===resourceId?resourceTitle.name:'网站详情'}</strong></nav>:<strong className="header-page-title">{pages[page]}</strong>}</Space>
      <Button className="global-search-trigger" aria-label="全局搜索" icon={<SearchOutlined/>} onClick={()=>setSearchOpen(true)}><span>搜索网站、问题、资料</span><kbd>Ctrl K</kbd></Button>
      <Space className="app-header-actions" size="small"><ThemeToggleButton compact />
        <Button className={`assistant-icon ${page==='support'?'selected':''}`} aria-label="智能维护助手" title="智能维护助手" type="primary"
          aria-current={page==='support'?'page':undefined} icon={<RobotOutlined aria-hidden="true"/>} onClick={()=>setPage('support')}>
          <span className="assistant-label">智能助手</span>
        </Button>
        <TodoCenter canWrite={canWrite}/>
        <Dropdown trigger={['click']} menu={{items:[
          {key:'identity',label:<span className="account-identity">{auth.username} · {roleLabel}</span>,disabled:true},
          {type:'divider' as const},
          {key:'model',label:'模型设置'}, {key:'usage',label:'今日用量'}, {key:'capabilities',label:'查看能力状态'},
          ...(!auth.is_guest?[{key:'password',icon:<LockOutlined/>,label:'修改密码'}]:[]),
          {type:'divider' as const}, {key:'logout',icon:<LogoutOutlined/>,label:auth.is_guest?'退出访客':'退出登录'},
        ],onClick:({key})=>{
          if(key==='model')setModelOpen(true);
          if(key==='usage')setUsageOpen(true);
          if(key==='capabilities')setCapabilitiesOpen(true);
          if(key==='password')setPasswordOpen(true);
          if(key==='logout')void logout();
        }}}><Button aria-label={`账号：${auth.username}`} title={`${auth.username} · ${roleLabel}`} type="text" className="account-button" icon={<UserOutlined aria-hidden="true"/>}><span className="account-name">{auth.username}</span></Button></Dropdown>
      </Space>
    </Layout.Header><Layout.Content className={`app-content ${page==='investigation'?'chat-content':''} ${page==='support'||(page==='issues'&&resourceId)?'support-content':''}`}>
      <ErrorBoundary resetKey={location.pathname}><Suspense fallback={<div className="page-loading"><Spin tip="正在加载工作区" /></div>}>
        {page==='home'&&<Home navigate={path=>navigate(path)} canWrite={canWrite} drillEnabled={drillEnabled===true}/>}
        {page==='websites'&&<Websites onTitle={siteTitle} assist={assist} ask={askHere} knowledge={()=>setPage('knowledge')} initialId={resourceId} opened={id=>navigate('/websites/'+id)}/>}
        {page==='issues'&&(resourceId?<PersonalSupport issueDetail entry={supportEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')} onIssues={()=>setPage('issues')} opened={id=>{if(id!==resourceId)navigate('/issues/'+id);}}/>:<Issues assist={entry=>navigate('/issues/'+entry.caseId)}/>)}
        {plainChat&&<AssistantChat id={parts[2]} open={id=>navigate(id?'/support/chat/'+id:'/support')}
          diagnose={()=>navigate('/support?mode=diagnose',{state:{fromChatId:parts[2]}})} model={model} configure={()=>setModelOpen(true)}/>}
        {page==='support'&&!plainChat&&<PersonalSupport entry={supportEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')}
          onChat={()=>navigate(fromChatId?'/support/chat/'+fromChatId:'/support')} onIssues={id=>navigate(id?'/issues/'+id:'/issues')}
          opened={id=>{if(id!==resourceId)navigate(id?'/support/'+id:'/support?mode=diagnose',{state:location.state});}}/>}
        {page==='investigation'&&drillEnabled===false&&<Navigate to='/home' replace/>}
        {page==='investigation'&&drillEnabled===true&&<Investigation onSource={setSource} canApprove={canWrite} initialId={resourceId} initialRunId={new URLSearchParams(location.search).get('run')||undefined} opened={id=>{if(id!==resourceId)navigate('/investigation/'+id);}}/>}
        {page==='knowledge'&&<OfficialLibrary onSource={setSource} assist={assist}/>}
        {page==='operations'&&(canWrite?<Operations canApprove={canWrite} drillEnabled={drillEnabled===true}/>:<Alert type="info" title="审批与工单" description="复核排查建议和变更风险，审批后记录处理结果。需要审批权限。"/>)}
        {page==='evaluation'&&(canReadEvaluation?<Evaluation/>:<Alert type="info" title="评测记录" description="查看检索命中、证据引用和诊断质量。需要平台管理员权限。"/>)}
      </Suspense></ErrorBoundary>
    </Layout.Content></Layout><SourceDrawer hit={source} close={()=>setSource(null)} ask={hit=>{setSource(null);askHere({...sideEntry,workflow:'consult',question:`请解释资料「${hit.doc_title}」${hit.applicable_version?`（适用版本：${hit.applicable_version}）`:''}中的检查方法、适用条件和证据要求；不要把手册当作本次故障现场证据。`});}}/><PasswordDialog open={passwordOpen} close={()=>setPasswordOpen(false)} changed={clearAuth}/>
    <Capabilities open={capabilitiesOpen} close={()=>setCapabilitiesOpen(false)} navigate={url=>navigate(url)}/>
    {modelOpen&&<AssistantSettings value={model} save={value=>{setModel(value);setModelOpen(false);}} close={()=>setModelOpen(false)}/>}
    {usageOpen&&<AssistantUsage close={()=>setUsageOpen(false)}/>}
    <CommandPalette key={auth.tenant_id+':'+auth.username} open={searchOpen} close={()=>setSearchOpen(false)} navigate={navigate} onSource={setSource} guest={!!auth.is_guest} canApprove={canWrite} admin={canReadEvaluation} drillEnabled={drillEnabled===true}/>
    <Drawer className="side-assistant" open={!!sideEntry} onClose={()=>setSideEntry(null)} title="随页维护助手" width={Math.min(1000, windowWidth)} destroyOnHidden>
      {sideEntry&&<ErrorBoundary resetKey={sideEntry.caseId||''}><Suspense fallback={<Spin/>}><PersonalSupport entry={sideEntry} onSource={setSource} onKnowledge={()=>setPage('knowledge')} onIssues={()=>setPage('issues')} opened={id=>setSideEntry(previous=>previous===sideEntry&&previous.caseId!==(id||undefined)?{...previous,caseId:id||undefined,question:undefined}:previous)}/></Suspense></ErrorBoundary>}
    </Drawer>
  </Layout>;
}
