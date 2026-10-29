# MySQL 慢查询治理

## 适用场景
慢查询日志数量上升，数据库 CPU / IO 使用率偏高，业务接口 RT 上涨。

## 排查步骤
1. `pt-query-digest` 或云监控聚合 top 慢 SQL。
2. `EXPLAIN` 检查执行计划：全表扫描、文件排序、索引失效。
3. 检查锁等待：`information_schema.innodb_trx`、`data_locks`。

## 处理方案
- 应急：KILL 高代价查询；对被打穿的接口临时限流。
- 治理：补充索引、改写 SQL、大事务拆小；引入读写分离。
- 预防：上线前 SQL 审核必须通过 EXPLAIN 评审。

## 相关错误码
- ERR_5001：查询超时
- ERR_5002：锁等待超时
