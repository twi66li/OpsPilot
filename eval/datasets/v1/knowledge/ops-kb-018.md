# Prometheus 告警状态与通知分工

告警表达式产生匹配元素时，对应标签集合的告警处于 active。for 要求条件在评估期间持续满足，未达到持续时长时为 pending，满足后转 firing。keep_firing_for 可在条件不再满足后维持 firing 一段时间。Prometheus 负责规则评估；Alertmanager 负责告警分组、静默与通知分发。

---
资料编号：ops-kb-018
适用范围：官方资料中文摘要；仅适用于标题产品，不代表本项目已集成。
来源：https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/
整理日期：2026-10-30
