import { defineConfig } from 'vite';

const reactPackages = /node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/;
const iconPackages = /node_modules[\\/]@ant-design[\\/]icons[\\/]/;

export default defineConfig({
  server: { proxy: { '/api': 'http://127.0.0.1:8000', '/auth': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000', '/ready': 'http://127.0.0.1:8000', '/live': 'http://127.0.0.1:8000' } },
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          // 将公共依赖优先放进 React 块，避免 React 反向引用图标块。
          groups: [
            { name: 'vendor-react', test: reactPackages, priority: 20 },
            { name: 'vendor-icons', test: iconPackages, priority: 10 },
          ],
        },
      },
    },
  },
});
