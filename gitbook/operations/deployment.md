# 部署

Rotor 可以直接作为 Python 进程运行，也可以使用仓库提供的 Docker 配置。

## 单进程部署

```bash
rotor serve --host 0.0.0.0 --port 8000
```

`/api/admin/*` 使用管理员账号和 Session 认证，但仍建议在 Rotor 前放置反向代理或
受控网络入口。部署前阅读[安全边界](security.md)。

## Docker 镜像

仓库 `Dockerfile` 基于 Python 3.12，通过 `pip install .` 安装包本身，因此运行时
依赖来自 `pyproject.toml`，`src/` 与已打包的 Alembic 迁移链一起进入镜像。镜像最终
运行：

```text
python -m rotor.cli serve --host 0.0.0.0 --port 8000
```

构建和启动：

```bash
docker build -t rotor .
mkdir -p "$HOME/.cache/rotor"

docker run --rm -p 8000:8000 \
  -v "$HOME/.cache/rotor:/data" \
  rotor
```

镜像默认使用 `/data/rotor.db`、`/data/conversations/` 和 `/data/logs/`。把宿主机
目录挂载到 `/data` 后，SQLite 数据库、Conversation store 和运行日志会一起持久化。

## Docker Compose

仓库 `docker-compose.yml` 启动单个 Rotor 容器并挂载 `./data`：

```bash
docker compose up -d
curl http://127.0.0.1:8000/health
```

Compose 默认只把端口绑定到 `127.0.0.1`，并把 SQLite 数据库、会话和日志统一写入
宿主机的 `./data/`。对外提供服务时，应在前面放置反向代理并设置
`ROTOR_ADMIN_COOKIE_SECURE=true`。

## 数据库

当前版本在启动时只支持**文件型 SQLite**。启动路径会执行打包的 Alembic revision 并
校验模型结构：能够识别的无版本旧库会先在原文件旁创建备份再接管；无法安全识别的库
会拒绝启动而不是猜测迁移。

`DATABASE_URL` 使用其他驱动（例如 `postgresql+asyncpg`）时，Rotor 会在启动阶段直接
报错退出，不会在缺少迁移的情况下继续运行。已有版本的数据库仍应在升级前执行部署
备份，详见[数据库与迁移](../development/database-migrations.md)。

## 多进程注意事项

adaptive 在线统计、渠道冷却状态和恢复探针占用都保存在进程内。多个 worker 之间不会
共享这些瞬时状态；数据库中的 usage 和路由决策仍会持久化。需要一致的在线路由行为
时，当前实现应以单个 Rotor 进程为边界，或接受各 worker 独立学习。
