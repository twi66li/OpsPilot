# ERR_5003 数据库连接池耗尽处理手册

## 适用场景
应用日志出现 `ERR_5003`（Cannot get connection from pool / connection pool exhausted），
或监控显示数据库活跃连接数长期等于 max_connections。

## 典型症状
- 接口大量 500/超时，堆栈中包含 `ERR_5003`
- `SHOW PROCESSLIST` 中大量 Sleep 连接
- 应用侧等待队列堆积，RT 从几十毫秒飙升至数秒

## 排查步骤
1. 确认连接池配置：`maxPoolSize`、`idleTimeout`、`maxWait` 是否与 DB 的 `max_connections` 匹配。
2. 检查是否存在慢查询占用连接：`SELECT * FROM information_schema.processlist WHERE time > 5;`
3. 检查是否有连接泄漏：连接未在 finally 中归还、事务未提交/回滚。
4. 检查是否发生流量突增或下游抖动导致的线程堆积。

## 处理方案
- 应急：先确认连接归属、事务与业务影响，经值班负责人批准后才可处置选定连接；不能批量 KILL 所有 Sleep 连接。
- 扩容：将 `max_connections` 上调 20%~50%，同时调大应用连接池 `maxPoolSize`。
- 治理：为慢 SQL 建立索引或加缓存；连接池开启 `leakDetectionThreshold`。

## 相关错误码
- ERR_5003：连接池耗尽
- ERR_5004：连接获取超时

## 预警指标
- MySQL Threads_connected / max_connections > 85% 告警
- 应用侧 acquire 等待时间 P99 > 100ms 告警

---
资料编号：ops-kb-001
适用范围：项目演示资料；期限、金额、错误码、阈值仅适用于本虚构场景。
来源：scripts/knowledge/ERR_5003_数据库连接池耗尽处理手册.md
整理日期：2026-10-30
