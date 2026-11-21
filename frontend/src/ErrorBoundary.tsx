import { Component, type ReactNode } from 'react';
import { Button, Result } from 'antd';

export function isChunkLoadError(error: unknown) {
  return error instanceof Error && /failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed|loading chunk .+ failed|^unable to preload CSS for /i.test(error.message);
}

// 页面级错误边界：单个页面渲染异常或懒加载分块失败时，只替换内容区，不让整站白屏。
// resetKey 变化（切换路由）时自动清除错误状态。
export default class ErrorBoundary extends Component<{resetKey: string; children: ReactNode}, {error: Error | null}> {
  state = {error: null as Error | null};

  static getDerivedStateFromError(error: Error) {
    return {error};
  }

  componentDidUpdate(previous: {resetKey: string}) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({error: null});
  }

  render() {
    const {error} = this.state;
    if (!error) return this.props.children;
    // 部署新版本后旧分块文件已不存在，刷新即可拿到新入口。
    const chunkMissing = isChunkLoadError(error);
    return <Result status="warning" title={chunkMissing ? '页面已更新，请刷新后继续' : '该页面暂时无法显示'}
      subTitle={chunkMissing ? '工作台刚发布了新版本，当前标签页仍在使用旧文件。' : '已保存的数据不受影响，可以重试或切换到其他页面。'}
      extra={[
        <Button key="reload" type="primary" onClick={() => window.location.reload()}>刷新页面</Button>,
        ...(!chunkMissing ? [<Button key="retry" onClick={() => this.setState({error: null})}>重试</Button>] : []),
      ]}/>;
  }
}
