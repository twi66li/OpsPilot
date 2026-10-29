# Kubernetes 三种健康探针

readiness 失败会使 Pod 未就绪，停止接受对应 Service 的流量，不代表容器一定被重启。liveness 连续失败达到阈值会触发重启。配置 startup 探针时，启动探针成功前不会执行其他探针；它适合慢启动应用。排查时先看 Pod 事件和探针失败原因，再区分启动慢、不能接流量与进程失活。

---
资料编号：ops-kb-013
适用范围：官方资料中文摘要；仅适用于标题产品，不代表本项目已集成。
来源：https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
整理日期：2026-10-30
