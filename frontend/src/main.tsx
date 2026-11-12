import { BrowserRouter } from 'react-router-dom';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{
    token: {colorPrimary:'#176f68',colorInfo:'#176f68',colorSuccess:'#388267',colorWarning:'#b7791f',colorError:'#bd514b',colorText:'#2b4047',colorTextSecondary:'#71848b',colorBorder:'#dfe7e6',colorBgLayout:'#f4f7f7',borderRadius:8,controlHeight:36,fontFamily:'"Microsoft YaHei", "Segoe UI", sans-serif'},
    components: {Button:{fontWeight:500,controlHeightSM:32},Card:{headerFontSize:15},Table:{headerBg:'#f7f9f9',headerColor:'#52676e',cellPaddingBlock:14},Menu:{darkItemBg:'#142a33',darkItemSelectedBg:'#24505a',darkItemColor:'#bed0d4'}},
  }}>
    <AntApp><BrowserRouter><App /></BrowserRouter></AntApp>
  </ConfigProvider></React.StrictMode>,
);
