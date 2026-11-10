import React from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{token: {colorPrimary: '#176b70', borderRadius: 8, fontFamily: '"Microsoft YaHei", "Segoe UI", sans-serif'}}}>
    <AntApp><App /></AntApp>
  </ConfigProvider></React.StrictMode>,
);
