# SQL 超时
statement_timeout 限制单条 SQL 语句的执行时间，不控制数据库连接池大小。
超时后回滚当前事务，并保留错误日志用于诊断。
