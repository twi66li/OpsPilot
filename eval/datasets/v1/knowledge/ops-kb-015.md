# MySQL EXPLAIN 与 EXPLAIN ANALYZE

EXPLAIN 用于查看优化器执行计划。EXPLAIN ANALYZE 会实际运行语句并输出迭代器统计，可观察估计与实际行数、首行时间、执行时间和循环次数。因此 ANALYZE 不能当作完全不执行 SQL 的只读解释动作；在排障环境应先评估语句代价和业务影响。这里讨论的是 MySQL 8.4 的语义。

---
资料编号：ops-kb-015
适用范围：官方资料中文摘要；仅适用于标题产品，不代表本项目已集成。
来源：https://dev.mysql.com/doc/refman/8.4/en/explain.html
整理日期：2026-10-30
