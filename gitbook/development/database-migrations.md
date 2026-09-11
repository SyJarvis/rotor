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
session_leases
session_lease_events
```

模型位于 `src/rotor/models/`。新增模型后，还必须在应用启动导入路径和
`src/rotor/migrations/env.py` 中确保 metadata 能发现它。

## 数据库驱动

应用运行时使用 `sqlite+aiosqlite`。当前版本的启动迁移路径只支持**文件型 SQLite**：
`DATABASE_URL` 使用其他驱动时，Rotor 会在启动阶段报错退出，而不会在缺少迁移的情况
下继续运行。Alembic 会把异步 URL 转换为同步驱动 URL（`sqlite`）。

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
- SQLite 的 ALTER TABLE 能力限制与批量迁移写法；
- 大表变更的锁和回滚成本；
- response route 与 usage 数据是否需要回填。

## SQLite 启动迁移

应用启动时会运行打包在 `rotor:migrations` 中的 Alembic revision，升级到
`head` 后再检查数据库结构与模型是否一致，不再使用 `create_all` 升级数据库。

对尚无 `alembic_version` 的旧 SQLite 数据库，Rotor 只接管能够精确识别的历史
结构。写入版本号和升级前，会在数据库文件旁通过 SQLite backup API 创建
`*.pre-alembic-<timestamp>.bak` 备份；无法识别的结构会中止启动且不会写入数据库。
已有 Alembic 版本的数据库不会自动生成这份接管备份，升级前仍应按部署流程备份。
