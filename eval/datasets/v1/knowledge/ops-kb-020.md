# 运维工单审批与恢复边界

用户请求创建工单时先生成草稿并保存审批单，LangGraph interrupt 暂停流程。admin 或 engineer 可审批，viewer 不可审批；批准后创建工单，拒绝不创建。相同审批单不允许重复决定。当前 MemorySaver 只保存进程内断点；审批单落库不等于图断点持久化，进程重启丢失等待状态时不能声称自动恢复，应按接口冲突处理。

---
资料编号：ops-kb-020
适用范围：当前代码实现摘要；不代表生产系统能力。
来源：app/services/agent_service.py、app/agent/graph.py、app/api/router_agent.py
整理日期：2026-10-30
