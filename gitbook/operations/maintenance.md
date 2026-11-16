# 维护命令

`rotor` 命令提供启动服务与一个受保护的数据维护操作。所有子命令都需要能够读取
`DATABASE_URL` 指向的文件型 SQLite 数据库。

## 启动服务

```bash
rotor serve --host 127.0.0.1 --port 8000
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 绑定地址 |
| `--port` | `8000` | 绑定端口 |
| `--reload` | 关闭 | 开发时启用 uvicorn 自动重载 |

## 清理空流式请求记录

早期版本曾出现一批成功但用量全部为零的网关记录。`cleanup-empty-requests` 只针对
这个已知批次，不是通用的“清理零用量”工具，也不清理其他模型或渠道的数据。

匹配条件同时要求：

- `status = 'success'`、`usage_source = 'missing'`；
- 所有 Token 计数为 0；
- `model = 'gpt-5.6-sol'`、`channel_id = 7`、`request_protocol = 'openai_chat'`。

默认只读，先看会命中多少行：

```bash
rotor cleanup-empty-requests --dry-run
```

输出示例：

```text
mode: dry-run
matched usage_ledger rows: 0
mapped request_logs rows: 0
related request_attempts rows: 0
related routing_decisions rows: 0
related conversation_records rows: 0
```

真正执行需要同时提供 `--apply` 和 `--yes`。命令会先把命中的记录写入一个独立的
SQLite 归档文件，再在同一事务里删除；归档写入失败时不会删除任何行。归档保留原表
行、候选映射和记录匹配条件的 `cleanup_metadata`。

```bash
rotor cleanup-empty-requests --apply --yes \
  --archive ./empty-requests-archive.sqlite
```

| 参数 | 说明 |
| --- | --- |
| `--database` | SQLite 文件路径；省略时使用 `DATABASE_URL` 指向的文件 |
| `--archive` | 归档 SQLite 文件路径；省略时在数据库旁生成带时间戳的默认文件名 |
| `--dry-run` | 只统计，不写入，默认行为 |
| `--apply --yes` | 归档并删除命中的记录 |

`--dry-run` 与 `--apply` 互斥。归档路径不能是数据库本身，也不能覆盖已有文件。

执行前应先停止写入并备份数据库：

```bash
rotor serve  # 另一个终端，Ctrl-C 停止
cp ~/.cache/rotor/rotor.db ~/.cache/rotor/rotor.db.backup
```

命令只接受 SQLite；`DATABASE_URL` 指向其他驱动时会以
`cleanup-empty-requests failed: ...` 退出。

## 备份与恢复

Rotor 的运行数据由四部分组成，完整备份需要一起复制：

```text
~/.cache/rotor/rotor.db          # 渠道、账户、Key、日志、用量
~/.cache/rotor/chats/            # 用户网页对话与附件
~/.cache/rotor/conversations/    # 网关请求记录
~/.rotor/settings.json           # 运行时路由设置与显示时区
```

停止 Rotor 后复制这些路径即可；恢复时覆盖回原位置再启动。升级前请按
[数据库与迁移](../development/database-migrations.md)的说明保存数据库备份。
