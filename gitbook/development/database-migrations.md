# 数据库与迁移

Rotor 使用 SQLAlchemy 异步引擎运行服务，并使用 Alembic 管理显式数据库迁移。

## 数据模型

主要表包括：

```text
channels
tokens
request_logs
conversation_records
usage_ledger
response_routes
routing_decisions
```

模型位于 `src/rotor/models/`。新增模型后，还必须在应用启动导入路径和
`alembic/env.py` 中确保 metadata 能发现它。

## 数据库驱动

应用运行时使用：

- SQLite：`sqlite+aiosqlite`
- PostgreSQL：`postgresql+asyncpg`

Alembic 会把异步 URL 转换为同步驱动 URL。SQLite 使用 `sqlite`；PostgreSQL
使用 `postgresql+psycopg`，因此执行 PostgreSQL 迁移的环境需要安装对应的同步
驱动。

## 查看迁移

在设置目标 `DATABASE_URL` 后：

```bash
alembic current
alembic history
```

升级前备份数据库，并在测试环境验证：

```bash
alembic upgrade head
```

## 创建迁移

修改 SQLAlchemy 模型后生成候选迁移：

```bash
alembic revision --autogenerate -m "describe change"
```

自动生成结果必须人工检查，尤其关注：

- 新列的默认值和可空性；
- 唯一约束和外键删除行为；
- SQLite 与 PostgreSQL 的差异；
- 大表变更的锁和回滚成本；
- response route 与 usage 数据是否需要回填。

应用启动时的 `create_all` 只会创建缺失结构，不会替代已有数据库的演进迁移。
