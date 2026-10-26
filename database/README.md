# Database
M0 API 使用 Mock repository，数据库仅有模型和迁移，不自动 seed。
首次：docker compose run --rm migrate 或 python -m alembic upgrade head。
离线检查：python -m alembic upgrade head --sql。
回滚模型：python -m alembic downgrade base（演示专用数据库）。
已有 volume 不重跑 init SQL，迁移同样执行 CREATE EXTENSION IF NOT EXISTS vector。
schema 创建账号需要 CREATE EXTENSION 权限；生产账号与迁移账号拆分在后续阶段。

