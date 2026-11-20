import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Button } from 'antd';

export type ThemeMode = 'light' | 'dark';

const STORAGE_KEY = 'opspilot.theme';

interface ThemeContextValue {
  mode: ThemeMode;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue>({ mode: 'light', toggle: () => {} });

function readInitialMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch {
    // 浏览器禁用存储时回退浅色模式
  }
  return 'light';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(readInitialMode);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', mode);
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      // 忽略存储写入限制
    }
  }, [mode]);

  const toggle = () => setMode(prev => (prev === 'dark' ? 'light' : 'dark'));

  return <ThemeContext.Provider value={{ mode, toggle }}>{children}</ThemeContext.Provider>;
}

export function useThemeMode() {
  return useContext(ThemeContext);
}

export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <span className="brand-logo" style={{ width: size, height: size }} aria-hidden="true">
      <svg width={Math.round(size * 0.6)} height={Math.round(size * 0.6)} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2L3 7v6c0 5.5 3.8 8.7 9 10 5.2-1.3 9-4.5 9-10V7l-9-5z" />
        <path d="M9 12l2 2 4-4" />
      </svg>
    </span>
  );
}

export function ThemeToggleButton({ compact = false }: { compact?: boolean }) {
  const { mode, toggle } = useThemeMode();
  const isDark = mode === 'dark';
  const label = isDark ? '切换为浅色模式' : '切换为深色模式';
  return (
    <Button
      type={compact ? 'text' : 'default'}
      size={compact ? 'middle' : 'small'}
      className={`theme-toggle-btn ${compact ? 'compact' : ''}`}
      aria-label={label}
      title={label}
      onClick={toggle}
      icon={
        isDark ? (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
          </svg>
        ) : (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
          </svg>
        )
      }
    >
      {!compact && (isDark ? '浅色模式' : '深色模式')}
    </Button>
  );
}
