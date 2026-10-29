# MySQL 8.4 慢查询日志判断条件

慢查询日志用于定位耗时 SQL，MySQL 8.4 默认未开启。记录受 long_query_time 与 min_examined_row_limit 等条件控制；不是把所有 SQL 都记录下来。获取初始锁的时间不计入文档所述执行时间，语句执行并释放锁后才写日志，所以日志顺序未必等于实际执行顺序。mysqldumpslow 可帮助汇总慢日志。

---
资料编号：ops-kb-014
适用范围：官方资料中文摘要；仅适用于标题产品，不代表本项目已集成。
来源：https://dev.mysql.com/doc/refman/8.4/en/slow-query-log.html
整理日期：2026-10-30
