import { useEffect, useRef, useState } from 'react';
import { Button, Tag } from 'antd';
import {
  CaretRightOutlined,
  CheckCircleFilled,
  DownOutlined,
  ExperimentOutlined,
  FileSearchOutlined,
  PauseOutlined,
  SafetyCertificateOutlined,
  UpOutlined,
} from '@ant-design/icons';

const STORAGE_KEY = 'opspilot.drill-demo-collapsed';
const VIDEO_SRC = '/media/drill-demo.mp4';
const VIDEO_POSTER = '/media/drill-demo-poster.jpg';
const VIDEO_DURATION = 30;

interface Stage {
  id: number;
  start: number;
  end: number;
  shortLabel: string;
  tag: string;
  tagColor: string;
  subtitle: string;
}

const STAGES: Stage[] = [
  {
    id: 0,
    start: 0,
    end: 5,
    shortLabel: '1. 异常告警',
    tag: '发现异常',
    tagColor: 'error',
    subtitle: '“咦？演练服务异常啦，我马上查查！”',
  },
  {
    id: 1,
    start: 5,
    end: 12,
    shortLabel: '2. 证据核对',
    tag: '只读取证',
    tagColor: 'processing',
    subtitle: '“指标、日志、运维手册全部翻出来核对，找到啦！”',
  },
  {
    id: 2,
    start: 12,
    end: 18,
    shortLabel: '3. 等待审批',
    tag: '人工审批门禁',
    tagColor: 'warning',
    subtitle: '“方案递给右侧审批，批准前我乖乖不动手哦。”',
  },
  {
    id: 3,
    start: 18,
    end: 24,
    shortLabel: '4. 批准恢复',
    tag: '执行固定预案',
    tagColor: 'cyan',
    subtitle: '“耶，审批通过！立刻按固定预案执行恢复！”',
  },
  {
    id: 4,
    start: 24,
    end: 30,
    shortLabel: '5. 复测通过',
    tag: '样本验证闭环',
    tagColor: 'success',
    subtitle: '“再连测三次请求，全部正常，验证通过啦！”',
  },
];

function getStageIndex(seconds: number): number {
  const index = STAGES.findIndex(item => seconds >= item.start && seconds < item.end);
  return index >= 0 ? index : STAGES.length - 1;
}

function readCollapsedPreference(guest: boolean): boolean {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'true') return true;
    if (saved === 'false') return false;
  } catch {
    // 浏览器禁用存储时使用默认展开策略
  }
  // 访客与首次进入空工作区默认展开演示
  return !guest && false;
}

function VolumeIcon({ muted }: { muted: boolean }) {
  return muted ? (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <line x1="23" y1="9" x2="17" y2="15" />
      <line x1="17" y1="9" x2="23" y2="15" />
    </svg>
  ) : (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
      <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
    </svg>
  );
}

export default function DrillWalkthrough({ guest, hasActiveChat }: { guest: boolean; hasActiveChat: boolean }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [collapsed, setCollapsed] = useState<boolean>(() => readCollapsedPreference(guest));
  const [manualToggle, setManualToggle] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [muted, setMuted] = useState(true);
  const [videoError, setVideoError] = useState(false);

  // 进入真实会话后自动收起演示，避免挤压对话区；用户手动点展开则尊重手动状态
  const isCollapsed = collapsed || (hasActiveChat && !manualToggle);

  const stageIndex = getStageIndex(currentTime);
  const stage = STAGES[stageIndex];

  // 与 30s 视频帧保持细腻同步（100ms 采样足够平滑且低开销）
  useEffect(() => {
    if (isCollapsed || videoError) return;
    const timer = window.setInterval(() => {
      const video = videoRef.current;
      if (!video) return;
      setCurrentTime(video.currentTime);
      setPlaying(!video.paused);
    }, 100);
    return () => window.clearInterval(timer);
  }, [isCollapsed, videoError]);

  function toggleCollapsed() {
    const next = !isCollapsed;
    setCollapsed(next);
    setManualToggle(!next);
    try {
      localStorage.setItem(STORAGE_KEY, String(next));
    } catch {
      // 忽略存储异常
    }
  }

  function jumpToStage(target: Stage) {
    setCurrentTime(target.start + 0.05);
    const video = videoRef.current;
    if (!video || videoError) return;
    video.currentTime = target.start;
    void video.play().then(() => setPlaying(true)).catch(() => {});
  }

  function togglePlay() {
    const video = videoRef.current;
    if (!video || videoError) return;
    if (video.paused) {
      void video.play().then(() => setPlaying(true)).catch(() => {});
    } else {
      video.pause();
      setPlaying(false);
    }
  }

  function toggleMute() {
    const next = !muted;
    setMuted(next);
    const video = videoRef.current;
    if (video) {
      video.muted = next;
      if (!next && video.paused) {
        void video.play().then(() => setPlaying(true)).catch(() => {});
      }
    }
  }

  // 根据时间轴派生各代码模块状态（与 test.py 30 秒分镜严格对齐）
  const serviceRecovered = currentTime >= 24;
  const serviceRecovering = currentTime >= 18.8 && currentTime < 24;
  const metricsVisible = currentTime >= 5.2;
  const logsVisible = currentTime >= 7.2;
  const runbookVisible = currentTime >= 9.2;
  const approvalPending = currentTime >= 12 && currentTime < 18;
  const approvalApproved = currentTime >= 18;
  const faultTurnedOff = currentTime >= 19.2;
  // 24–30s 对应小鲸鱼娘数三下，三颗复测 200 状态码依次亮绿
  const probeCount = currentTime < 24 ? 0 : Math.min(3, Math.floor((currentTime - 24) / 1.15) + 1);
  const verifiedDone = currentTime >= 27.2;

  if (isCollapsed) {
    return (
      <section className="drill-demo-collapsed" aria-label="故障演练流程演示（已收起）">
        <div className="drill-demo-collapsed-info">
          <ExperimentOutlined className="drill-demo-icon" />
          <strong>故障演练原理演示</strong>
          <span className="muted">异常告警 → 只读取证 → 人工审批 → 固定预案恢复 → 三次请求复测</span>
        </div>
        <Button size="small" icon={<DownOutlined />} onClick={toggleCollapsed}>
          展开演示
        </Button>
      </section>
    );
  }

  const leftStatusClass = serviceRecovered ? 'ok' : serviceRecovering ? 'warn' : 'err';
  const rightStatusClass = verifiedDone ? 'ok' : approvalApproved ? 'accent' : approvalPending ? 'warn' : 'idle';
  const progressPct = Math.min(100, Math.max(0, (currentTime / VIDEO_DURATION) * 100));

  return (
    <section className="drill-demo-shell" aria-label="故障演练流程演示">
      <header className="drill-demo-topbar">
        <div className="drill-demo-title">
          <ExperimentOutlined className="drill-demo-icon" />
          <strong>故障演练闭环演示</strong>
          <Tag color={stage.tagColor}>{stage.tag}</Tag>
        </div>
        <nav className="drill-demo-steps" aria-label="演练阶段切换">
          {STAGES.map((item, idx) => (
            <button
              key={item.id}
              type="button"
              className={`drill-step-pill ${idx === stageIndex ? 'active' : idx < stageIndex ? 'done' : ''}`}
              onClick={() => jumpToStage(item)}
            >
              {item.shortLabel}
            </button>
          ))}
        </nav>
        <div className="drill-demo-controls">
          <Button
            size="small"
            type="text"
            aria-label={playing ? '暂停演示' : '播放演示'}
            title={playing ? '暂停演示' : '播放演示'}
            icon={playing ? <PauseOutlined /> : <CaretRightOutlined />}
            onClick={togglePlay}
            disabled={videoError}
          />
          <Button
            size="small"
            type="text"
            aria-label={muted ? '开启配音' : '静音'}
            title={muted ? '开启配音' : '静音'}
            icon={<VolumeIcon muted={muted} />}
            onClick={toggleMute}
            disabled={videoError}
          >
            <span className="drill-control-label">{muted ? '开声音' : '配音中'}</span>
          </Button>
          <Button size="small" type="text" icon={<UpOutlined />} onClick={toggleCollapsed}>
            收起
          </Button>
        </div>
      </header>

      <div className="drill-demo-stage">
        {/* 左栏：独立测试环境与只读证据链 */}
        <div className="drill-col drill-col-left">
          <div className="drill-col-head">
            <span className={`drill-dot ${leftStatusClass}`} />
            <span>独立测试环境</span>
          </div>

          <div className={`drill-card ${stageIndex === 0 || approvalPending ? 'highlight-err' : serviceRecovered ? 'highlight-ok' : ''}`}>
            <div className="drill-card-head">
              <ExperimentOutlined />
              <div>
                <strong>测试服务状态</strong>
                <small>order-service · 演练沙箱</small>
              </div>
              <span className={`drill-http-badge ${serviceRecovered ? 'ok' : serviceRecovering ? 'warn' : 'err'}`}>
                {serviceRecovered ? '200 OK' : serviceRecovering ? '恢复中' : '500 ERR'}
              </span>
            </div>
            <div className="drill-code-block">
              <div className="drill-code-row">
                <span className="k">GET</span>
                <code>/api/orders</code>
                <span className={`v ${serviceRecovered ? 'ok' : 'err'}`}>
                  {serviceRecovered ? '-> 200' : '-> 500'}
                </span>
              </div>
              <div className="drill-code-row">
                <span className={`drill-pill ${serviceRecovered ? 'ok' : 'err'}`}>
                  {serviceRecovered ? 'pool_wait: 18ms' : 'ERR_5003: db_pool_wait_timeout'}
                </span>
              </div>
              <div className="drill-code-row">
                <span className="k">fault_injected:</span>
                <code className={faultTurnedOff ? 'ok' : 'err'}>{faultTurnedOff ? 'false' : 'true'}</code>
                {approvalPending && <span className="drill-lock-note">待审批 · 未变更</span>}
              </div>
            </div>
          </div>

          <div className={`drill-card ${stageIndex === 1 ? 'highlight-accent' : ''}`}>
            <div className="drill-card-head">
              <FileSearchOutlined />
              <div>
                <strong>只读工具证据链</strong>
                <small>只查证不修改 · 证据可追溯</small>
              </div>
            </div>
            <ul className="drill-tool-list">
              <li className={metricsVisible ? 'visible' : ''}>
                <code>query_service_metrics()</code>
                <span className="drill-pill info">err_rate: 42%</span>
              </li>
              <li className={logsVisible ? 'visible' : ''}>
                <code>query_logs()</code>
                <span className="drill-pill err">ERR_5003 × 18</span>
              </li>
              <li className={runbookVisible ? 'visible' : ''}>
                <code>search_runbook()</code>
                <span className="drill-pill accent">连接池超时手册</span>
              </li>
            </ul>
          </div>
        </div>

        {/* 中栏：纯白底 Q 版鲸鱼娘同步舞台 */}
        <div className="drill-col-center">
          <div className="drill-video-frame">
            {videoError ? (
              <div className="drill-video-fallback">
                <strong>{stage.shortLabel}</strong>
                <p>{stage.subtitle}</p>
              </div>
            ) : (
              <video
                ref={videoRef}
                className="drill-video"
                src={VIDEO_SRC}
                poster={VIDEO_POSTER}
                autoPlay
                loop
                muted={muted}
                playsInline
                preload="metadata"
                onError={() => setVideoError(true)}
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
              />
            )}
          </div>
          <div className="drill-subtitle-bar" aria-live="polite">
            <span className="drill-subtitle-text">{stage.subtitle}</span>
            <div className="drill-progress-track" aria-hidden="true">
              <div className="drill-progress-fill" style={{ width: `${progressPct}%` }} />
            </div>
          </div>
        </div>

        {/* 安全边界竖线（借鉴 Magpie 的边界表达） */}
        <div className={`drill-boundary ${approvalPending ? 'pending' : approvalApproved ? 'approved' : ''}`} aria-hidden="true">
          <span>审批边界</span>
        </div>

        {/* 右栏：人工审批门禁与样本复测闭环 */}
        <div className="drill-col drill-col-right">
          <div className="drill-col-head">
            <span className={`drill-dot ${rightStatusClass}`} />
            <span>审批与恢复</span>
          </div>

          <div className={`drill-card ${approvalPending ? 'highlight-warn' : approvalApproved ? 'highlight-ok' : 'dimmed'}`}>
            <div className="drill-card-head">
              <SafetyCertificateOutlined />
              <div>
                <strong>修复申请单</strong>
                <small>写操作必须经人工批准</small>
              </div>
              {approvalApproved ? (
                <span className="drill-http-badge ok">已批准</span>
              ) : approvalPending ? (
                <span className="drill-http-badge warn">待审批</span>
              ) : (
                <span className="drill-http-badge idle">待提交</span>
              )}
            </div>
            <div className="drill-code-block">
              <div className="drill-code-row">
                <span className="k">action:</span>
                <span className="drill-pill accent">&quot;reset_demo_fault&quot;</span>
              </div>
              <div className="drill-code-row">
                <span className="k">target:</span>
                <code>order-service (test)</code>
              </div>
              <div className="drill-code-row">
                <span className="k">status:</span>
                <code className={approvalApproved ? 'ok' : approvalPending ? 'warn' : ''}>
                  {approvalApproved ? 'APPROVED' : approvalPending ? 'PENDING_APPROVAL' : 'DRAFT'}
                </code>
              </div>
            </div>
            <div className={`drill-mock-approve ${approvalPending ? 'pulse' : approvalApproved ? 'done' : ''}`}>
              {approvalApproved ? (
                <>
                  <CheckCircleFilled /> 人工已批准执行
                </>
              ) : (
                '批准并执行固定预案'
              )}
            </div>
          </div>

          <div className={`drill-card ${stageIndex >= 3 ? 'highlight-ok' : 'dimmed'}`}>
            <div className="drill-card-head">
              <CheckCircleFilled />
              <div>
                <strong>执行与样本复测</strong>
                <small>关闭故障注入并核对响应</small>
              </div>
            </div>
            <div className="drill-code-block">
              <div className="drill-code-row">
                <span className="k">fault_injection:</span>
                <span className={`drill-pill ${faultTurnedOff ? 'ok' : 'idle'}`}>
                  {faultTurnedOff ? 'ON → OFF' : 'ON'}
                </span>
              </div>
              <div className="drill-probe-grid">
                <div className="drill-probe-row">
                  <span className="k">恢复前</span>
                  <div className="drill-probe-codes">
                    <span className="probe-code err">500</span>
                    <span className="probe-code err">500</span>
                    <span className="probe-code err">500</span>
                  </div>
                </div>
                <div className="drill-probe-row">
                  <span className="k">恢复后</span>
                  <div className="drill-probe-codes">
                    {[0, 1, 2].map(index => (
                      <span
                        key={index}
                        className={`probe-code ${index < probeCount ? 'ok active' : 'idle'}`}
                      >
                        {index < probeCount ? '200' : '···'}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
            <div className={`drill-verify-foot ${verifiedDone ? 'done' : ''}`}>
              <CheckCircleFilled />
              <span>{verifiedDone ? '写操作经人工审批，3 次复测通过' : '等待审批后自动发起 3 次探针复测'}</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
