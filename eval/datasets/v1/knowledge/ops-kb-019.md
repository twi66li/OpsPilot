# 诊断工具的输入与证据边界

诊断工具按服务标识或中文别名提取实体，订单服务映射为 order-service，网关映射为 api-gateway-service。query_service_metrics 提供近一小时 QPS、P99 延迟、错误率和 CPU 的演示指标，一次最多查询两个服务。query_fault_history 按租户、服务和错误码检索故障，精确匹配为空时退回同租户最近记录；最近记录不能冒充同一故障的已确认根因。工具结果是样例，不是已接入真实监控系统。

---
资料编号：ops-kb-019
适用范围：当前代码实现摘要；不代表生产系统能力。
来源：app/agent/tools.py、app/services/metrics_service.py
整理日期：2026-10-30
