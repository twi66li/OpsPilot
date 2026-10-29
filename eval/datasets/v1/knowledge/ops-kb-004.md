# Pod OOMKilled 处理指南

## 适用场景
Kubernetes 中应用容器反复重启，`kubectl describe pod` 显示 `OOMKilled`，
或应用日志出现 `java.lang.OutOfMemoryError` / Python `MemoryError`。

## 排查步骤
1. 确认容器内存 limit 与实际使用曲线，判断是泄漏还是配额不足。
2. 导出内存快照（heap dump / tracemalloc）定位大对象。
3. 检查近期发布变更：缓存无界增长、大结果集一次性加载、并发突增。

## 处理方案
- 应急：核对节点余量和工作负载后，经批准调整内存 limit；流量大时评估扩容副本，不能机械增加固定百分比。
- 治理：修复内存泄漏；为缓存设置 TTL 与容量上限；分页/流式处理大数据集。
- 预防：配置内存使用率 80% 告警；压测验证 limit 余量。

## 相关错误码
- ERR_9001：容器 OOMKilled
- ERR_9002：JVM Old 区使用率超阈值

---
资料编号：ops-kb-004
适用范围：项目演示资料；期限、金额、错误码、阈值仅适用于本虚构场景。
来源：scripts/knowledge/Pod_OOMKilled处理指南.md
整理日期：2026-10-30
