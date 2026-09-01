# 部署

Rotor 可以直接作为 Python 进程运行，也可以使用仓库提供的 Docker 配置。

## 单进程部署

```bash
rotor serve --host 0.0.0.0 --port 8000
```

生产部署应在 Rotor 前放置反向代理或受控网络入口，尤其是因为管理 API 没有独立
的管理员认证。部署前阅读[安全边界](security.md)。

## Docker 镜像

仓库 `Dockerfile` 基于 Python 3.11，安装 `requirements.txt`，复制 `src/` 和
Alembic 文件，最终运行：

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

仓库 `docker-compose.yml` 同时启动 Rotor 和 PostgreSQL 15：

```bash
docker compose up -d
curl http://127.0.0.1:8000/health
```

示例 Compose 中的数据库用户名和密码是开发默认值，公开部署前必须替换，并避免
把 PostgreSQL 的 `5432` 端口暴露到不受信任网络。Compose 使用 PostgreSQL volume
保存主数据库，并将 Conversation store 和运行日志保存到宿主机的 `./data/`。

## 数据库初始化

当前 SQLite 启动路径会自动执行打包的 Alembic revision 并检查模型结构。能够
识别的无版本旧库会先在原文件旁创建备份再接管；已有版本的生产数据库仍应在
升级前执行部署备份，详见[数据库与迁移](../development/database-migrations.md)。

## 多进程注意事项

adaptive 在线统计和渠道冷却状态保存在进程内。多个 worker 之间不会共享这些
瞬时状态；数据库中的 usage 和路由决策仍会持久化。需要一致的在线路由行为时，
当前实现应以单个 Rotor 进程为边界，或接受各 worker 独立学习。
