import { useId } from 'react';
import { formatTime } from './api';
import type { ProbePoint } from './types';

function colorOf(point: ProbePoint) {
  if (point.state === 'normal') return 'var(--ok)';
  if (point.timed_out) return 'var(--warn)';
  if (point.state === 'abnormal') return 'var(--err)';
  return 'var(--text-3)';
}

export default function ProbeTrendChart({ points }: { points: ProbePoint[] }) {
  const gradId = useId();
  if (!points.length) return null;
  const recent = [...points]
    .sort((a, b) => (a.collected_at || '').localeCompare(b.collected_at || ''))
    .slice(-20);
  const latencies = recent.map(p => (typeof p.elapsed_ms === 'number' && Number.isFinite(p.elapsed_ms) ? p.elapsed_ms : 0));
  const maxMs = Math.max(100, ...latencies);
  const normalCount = recent.filter(p => p.state === 'normal').length;
  const validLat = recent.map(p => p.elapsed_ms).filter((v): v is number => typeof v === 'number' && v >= 0);
  const avgMs = validLat.length ? Math.round(validLat.reduce((s, v) => s + v, 0) / validLat.length) : 0;

  const width = 440;
  const height = 124;
  const padX = 16;
  const padY = 16;
  const spanX = width - padX * 2;
  const spanY = height - padY * 2;
  const coords = recent.map((p, idx) => {
    const x = recent.length === 1 ? width / 2 : padX + (idx / (recent.length - 1)) * spanX;
    const val = typeof p.elapsed_ms === 'number' && p.elapsed_ms >= 0 ? p.elapsed_ms : 0;
    const y = padY + spanY - (val / maxMs) * spanY;
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), point: p };
  });
  const polyline = coords.map(c => `${c.x},${c.y}`).join(' ');
  const baseLine = height - padY;
  const areaPoints = coords.length > 1 ? `${coords[0].x},${baseLine} ${polyline} ${coords[coords.length - 1].x},${baseLine}` : '';

  return (
    <div className="probe-trend-wrap">
      <div className="probe-trend-meta">
        <span>最近 {recent.length} 次探测 · 峰值 {Math.round(maxMs)} ms · 平均 {avgMs} ms</span>
        <span>正常率 {Math.round((normalCount / recent.length) * 100)}%（{normalCount}/{recent.length}）</span>
      </div>
      <svg
        role="img"
        aria-label="网站 HTTP 探测耗时与状态趋势图"
        viewBox={`0 0 ${width} ${height}`}
        className="probe-trend-svg"
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        <line x1={padX} y1={padY} x2={width - padX} y2={padY} stroke="var(--line)" strokeDasharray="3 3" />
        <line x1={padX} y1={height / 2} x2={width - padX} y2={height / 2} stroke="var(--line)" strokeDasharray="3 3" />
        <line x1={padX} y1={baseLine} x2={width - padX} y2={baseLine} stroke="var(--line-strong)" />
        {areaPoints && <polygon fill={`url(#${gradId})`} points={areaPoints} />}
        {coords.length > 1 && <polyline fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinejoin="round" points={polyline} />}
        {coords.map(({ x, y, point }, index) => (
          <circle key={`${point.collected_at}-${index}`} cx={x} cy={y} r="4" fill={colorOf(point)} stroke="var(--panel)" strokeWidth="1.5">
            <title>
              {`${formatTime(point.collected_at)} · ${point.http_status ? `HTTP ${point.http_status}` : point.timed_out ? '超时' : point.state} · ${typeof point.elapsed_ms === 'number' ? `${point.elapsed_ms} ms` : '耗时未知'}`}
            </title>
          </circle>
        ))}
      </svg>
    </div>
  );
}
