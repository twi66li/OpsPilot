import { BrowserRouter } from 'react-router-dom';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import './styles.css';

const opsTheme = {
  token: {
    colorPrimary: '#0e6a61', colorInfo: '#2a6a8f', colorSuccess: '#2f7d57', colorWarning: '#a96a0f', colorError: '#b3403a',
    colorInfoBg: '#e3eff6', colorInfoBorder: '#b5d3e5', colorSuccessBg: '#e5f3ea', colorSuccessBorder: '#bddfc9',
    colorWarningBg: '#fbf0dc', colorWarningBorder: '#ecd3a3', colorErrorBg: '#fbe8e6', colorErrorBorder: '#efbdb8',
    colorBgBase: '#ffffff', colorBgLayout: '#f3f5f3', colorBgContainer: '#ffffff', colorBgElevated: '#ffffff',
    colorText: '#16252b', colorTextSecondary: '#4d6067', colorTextTertiary: '#66777d',
    colorBorder: '#c3ccc8', colorBorderSecondary: '#dde3e0',
    borderRadius: 6, controlHeight: 32, fontSize: 14,
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif',
    fontFamilyCode: '"Cascadia Mono", Consolas, "Microsoft YaHei", monospace',
  },
  components: {
    Button: { fontWeight: 500, controlHeightSM: 28, borderRadius: 5, defaultShadow: 'none', primaryShadow: 'none', dangerShadow: 'none' },
    Input: { borderRadius: 5 },
    Select: { borderRadius: 5 },
    Card: { headerFontSize: 14 },
    Table: { headerBg: '#f7f9f8', headerColor: '#4d6067', cellPaddingBlock: 12 },
  },
};

createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={opsTheme} button={{autoInsertSpace:false}}>
    <AntApp><BrowserRouter><App /></BrowserRouter></AntApp>
  </ConfigProvider></React.StrictMode>,
);
