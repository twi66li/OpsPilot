# Kubernetes 资源 requests 与 limits 的区别

requests 用于调度时的资源需求判断；limits 描述容器资源使用限制。CPU limit 通常通过节流约束；内存 limit 由内核在内存压力下以 OOM 终止等方式执行，不保证越界瞬间立刻终止。内存用量应与配置和实际曲线一起核对，不能把应用异常文本直接等同于容器 OOM。

---
资料编号：ops-kb-012
适用范围：官方资料中文摘要；仅适用于标题产品，不代表本项目已集成。
来源：https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
整理日期：2026-10-30
