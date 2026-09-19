# Redis 连接池
缓存连接池通过 redis_pool_size 配置，限制 Redis 客户端的并发连接。
它和 SQL 数据库连接池分别管理，不能共用连接。
