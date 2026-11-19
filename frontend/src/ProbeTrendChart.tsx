import { formatTime } from './api';
import type { ProbePoint } from './types';

function colorOf(point: ProbePoint) {
  if (point.state === 'normal') return '#16a34a';
  if (point.timed_out) return '#d97706';
  if (point.state === 'abnormal') return '#dc2626';
  return '#64748b';
}

export default function ProbeTrendChart({ points }: { points: ProbePoint[] }) {
  if (!points.length) return null;
  const recent = [...points]
    .sort((a, b) => (a.collected_at || '').localeCompare(b.collected_at || ''))
    .slice(-20);
  const latencies = recent.map(p => (typeof p.elapsed_ms === 'number' && Number.isFinite(p.elapsed_ms) ? p.elapsed_ms : 0));
  const maxMs = Math.max(100, ...latencies);
  const normalCount = recent.filter(p => p.state === 'normal').length;
  const validLat = recent.map(p => p.elapsed_ms).filter((v): v is number => typeof v === 'number' && v >= 0);
  const avgMs = validLat.length ? Math.round(validLat.reduce((s, v) => s + v, 0) / validLat.length) : 0;

  const width = 420;
  const height = 110;
  const padX = 16;
  const padY = 14;
  const spanX = width - padX * 2;
  const spanY = height - padY * 2;
  const coords = recent.map((p, idx) => {
    const x = recent.length === 1 ? width / 2 : padX + (idx / (recent.length - 1)) * spanX;
    const val = typeof p.elapsed_ms === 'number' && p.elapsed_ms >= 0 ? p.elapsed_ms : 0;
    const y = padY + spanY - (val / maxMs) * spanY;
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), point: p };
  });
  const polyline = coords.map(c => `${c.x},${c.y}`).join(' ');

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, fontSize: 12, color: '#475569', marginBottom: 6 }}>
        <span>最近 {recent.length} 次探测趋势（最高 {Math.round(maxMs)} ms · 平均 {avgMs} ms）</span>
        <span>正常率 {Math.round((normalCount / recent.length) * 100)}%（{normalCount}/{recent.length}）</span>
      </div>
      <svg
        role="img"
        aria-label="网站 HTTP 探测耗时与状态趋势图"
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: '100%', maxWidth: 520, height: 110, background: '#f8fafc', borderRadius: 6, border: '1px solid #e2e8f0', display: 'block' }}
      >
        <line x1={padX} y1={padY} x2={width - padX} y2={padY} stroke="#e2e8f0" strokeDasharray="3 3" />
        <line x1={padX} y1={height / 2} x2={width - padX} y2={height / 2} stroke="#e2e8f0" strokeDasharray="3 3" />
        <line x1={padX} y1={height - padY} x2={width - padX} y2={height - padY} stroke="#cbd5e1" />
        {coords.length > 1 && <polyline fill="none" stroke="#2563eb" strokeWidth="2" points={polyline} />}
        {coords.map(({ x, y, point }, index) => (
          <circle key={`${point.collected_at}-${index}`} cx={x} cy={y} r="4" fill={colorOf(point)} stroke="#fff" strokeWidth="1.5">
            <title>
              {`${formatTime(point.collected_at)} · ${point.http_status ? `HTTP ${point.http_status}` : point.timed_out ? '超时' : point.state} · ${typeof point.elapsed_ms === 'number' ? `${point.elapsed_ms} ms` : '耗时未知'}`}
            </title>
          </circle>
        ))}
      </svg>
    </div>
  );
}
