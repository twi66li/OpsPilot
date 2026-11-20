import { BrowserRouter } from 'react-router-dom';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import { ThemeProvider, useThemeMode } from './themeMode';
import './styles.css';
import './workbench-v3.css';

function buildOpsTheme(isDark: boolean) {
  return {
    algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: isDark
      ? {
          colorPrimary: '#2dd4bf',
          colorInfo: '#2dd4bf',
          colorSuccess: '#4ade80',
          colorWarning: '#fbbf24',
          colorError: '#f87171',
          colorBgBase: '#081114',
          colorBgLayout: '#081114',
          colorBgContainer: '#0f1b1f',
          colorBgElevated: '#132227',
          colorText: '#e7f2f0',
          colorTextSecondary: '#9ab5b2',
          colorTextTertiary: '#6c8986',
          colorBorder: '#243c43',
          colorBorderSecondary: '#1b3036',
          borderRadius: 8,
          controlHeight: 34,
          fontSize: 14,
          fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif',
          fontFamilyCode: '"Cascadia Mono", Consolas, "Microsoft YaHei", monospace',
        }
      : {
          colorPrimary: '#0d7368',
          colorInfo: '#0d7368',
          colorSuccess: '#1e7e57',
          colorWarning: '#a96a0f',
          colorError: '#b3403a',
          colorInfoBg: '#dff2ee',
          colorInfoBorder: '#a7d8cf',
          colorSuccessBg: '#e5f3ea',
          colorSuccessBorder: '#bddfc9',
          colorWarningBg: '#fbf0dc',
          colorWarningBorder: '#ecd3a3',
          colorErrorBg: '#fbe8e6',
          colorErrorBorder: '#efbdb8',
          colorBgBase: '#ffffff',
          colorBgLayout: '#eef4f3',
          colorBgContainer: '#ffffff',
          colorBgElevated: '#ffffff',
          colorText: '#102427',
          colorTextSecondary: '#486064',
          colorTextTertiary: '#647b7f',
          colorBorder: '#bfd2ce',
          colorBorderSecondary: '#d6e4e1',
          borderRadius: 8,
          controlHeight: 34,
          fontSize: 14,
          fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif',
          fontFamilyCode: '"Cascadia Mono", Consolas, "Microsoft YaHei", monospace',
        },
    components: {
      Button: { fontWeight: 500, controlHeightSM: 28, borderRadius: 7, defaultShadow: 'none', primaryShadow: 'none', dangerShadow: 'none' },
      Input: { borderRadius: 7 },
      Select: { borderRadius: 7 },
      Card: { headerFontSize: 14, borderRadiusLG: 10 },
      Table: {
        headerBg: isDark ? '#0b1418' : '#f4f8f7',
        headerColor: isDark ? '#9ab5b2' : '#486064',
        cellPaddingBlock: 12,
      },
    },
  };
}

function RootApp() {
  const { mode } = useThemeMode();
  return (
    <ConfigProvider locale={zhCN} theme={buildOpsTheme(mode === 'dark')} button={{ autoInsertSpace: false }}>
      <AntApp message={{ top: 68 }}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <RootApp />
    </ThemeProvider>
  </React.StrictMode>,
);
