# Rotor

[简体中文](README.md) | [English](README.en.md)

Rotor 是一个自托管的 LLM API 网关。它把多个模型供应商和 API Key 组织为 Channel，
向客户端提供统一的 OpenAI Chat Completions、OpenAI Responses、OpenAI Images 和
Anthropic Messages 接口，并负责协议转换、路由、故障转移、Token 记账和运行状态
观测。

Rotor 同时提供浏览器管理页面、管理 API、面向机器调用的只读 Control API，以及
独立的 `rotor-mcp` 诊断适配器。

## 主要能力

- **统一协议入口**：支持 `/v1/chat/completions`、`/v1/responses`、
  `/v1/images/generations` 和 `/anthropic/v1/messages`。
- **跨协议转换**：在 OpenAI Chat、Responses 和 Anthropic Messages 之间转换基础
  消息、流式事件、工具调用和用量信息。
- **多渠道路由**：按模型、协议、优先级、权重和运行状态选择 Channel，支持 fallback、
  会话亲和与自适应评分。
- **可观测性**：记录请求日志、每次渠道尝试、归一化错误、延迟、Token 用量和缓存
  Token，并在管理页面展示统计。
- **管理与诊断**：通过管理页面维护 Channel 和用户 Token；通过独立鉴权的 Control
  API 向 MCP、CLI 和自动化任务提供只读诊断数据。
- **本地优先存储**：默认使用 SQLite，也支持 PostgreSQL；会话和运行数据保存在本地。

## 工作方式

```text
OpenAI / Anthropic SDK、Codex、Claude Code、OpenCode
                         │
                         ▼
                    Rotor API
                         │
          路由、协议转换、fallback、记账
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   OpenAI 兼容渠道   Responses 渠道   Anthropic 渠道

rotor-mcp ── Control API ──> Rotor 诊断与用量事实
```

`rotor-mcp` 是独立 Python 包和独立进程，不直接访问 Rotor 数据库，也不导入 Rotor
后端模块。当前 Rotor Docker 镜像不包含或自动启动 MCP Server。

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- SQLite（默认）或 PostgreSQL

### 安装并启动

```bash
git clone https://github.com/SyJarvis/rotor.git
cd rotor

python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

rotor serve --host 0.0.0.0 --port 8000
```

检查服务：

```bash
curl http://127.0.0.1:8000/health
```

浏览器管理页面：

```text
http://127.0.0.1:8000/
```

默认运行数据位于：

```text
~/.cache/rotor/
├── rotor.db
└── conversations/
```

## Docker

使用 SQLite 启动单个 Rotor 容器：

```bash
docker build -t rotor .
mkdir -p "$HOME/.cache/rotor"

docker run --rm \
  -p 8000:8000 \
  -v "$HOME/.cache/rotor:/data" \
  rotor
```

镜像默认把 SQLite 数据库写入 `/data/rotor.db`，把 Conversation store 写入
`/data/conversations/`。上面的单一挂载会同时持久化两者，对应宿主机目录为
`~/.cache/rotor/`。

使用 Docker Compose 启动 Rotor 和 PostgreSQL：

```bash
docker compose up --build -d
docker compose logs -f rotor
```

仓库 Compose 使用开发用数据库凭据并暴露 PostgreSQL 端口，上线前必须修改。当前
Compose 使用 PostgreSQL volume 持久化主数据库，并把 Conversation store 挂载到
宿主机的 `./data/conversations/`。当前 Docker 配置只运行 Rotor；`rotor-mcp` 后续
应使用独立镜像运行。

## 完成第一次请求

### 1. 添加 Channel

打开管理页面，在“渠道”中添加供应商地址、API Key、模型和协议。也可以调用
`POST /api/admin/channels`：

```bash
curl -X POST http://127.0.0.1:8000/api/admin/channels \
  -H "Content-Type: application/json" \
  -d '{
    "name": "openai-compatible",
    "type": "openai",
    "key": "provider-api-key",
    "base_url": "https://provider.example/v1",
    "models": ["your-model"],
    "model_mapping": {},
    "priority": 10,
    "weight": 1,
    "enabled": true,
    "protocol": "openai"
  }'
```

`protocol` 描述上游实际使用的接口：

| 值 | 上游接口 |
| --- | --- |
| `openai` | Chat Completions |
| `openai_responses` | Responses |
| `anthropic` | Anthropic Messages |

Provider 类型负责鉴权规则，协议只决定请求路径和转换方式。

### 2. 创建 Rotor 用户 Token

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/admin/tokens/generate?name=demo&quota=1000000"
```

保存响应中的 `sk-` Token：

```bash
export ROTOR_API_KEY="sk-your-rotor-token"
```

### 3. 调用统一接口

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $ROTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Conversation-Id: demo-conversation" \
  -d '{
    "model": "your-model",
    "messages": [
      {"role": "user", "content": "你好，只回复 ROTOR_OK"}
    ]
  }'
```

Codex、Claude Code 和 OpenCode 的配置方式见
[客户端接入指南](gitbook/guides/client-integrations.md)。

## API 概览

| 范围 | 主要端点 |
| --- | --- |
| 服务状态 | `GET /health`、`GET /api` |
| OpenAI Chat | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` 及 Response 资源端点 |
| OpenAI Images | `POST /v1/images/generations` |
| Anthropic | `POST /anthropic/v1/messages`、`POST /anthropic/v1/messages/count_tokens` |
| 模型 | `GET /v1/models` |
| 管理 API | `/api/admin/channels`、`/api/admin/tokens`、`/api/admin/logs`、`/api/admin/settings` |
| Control API | `/api/control/v1/channels`、请求 trace、近期失败和模型用量 |

Responses API 支持创建、查询、取消、删除、输入项查询、输入 Token 计数和 compact。
当上游是原生 Responses Channel 时，Rotor 会把后续资源操作路由回原 Channel 和账号。

## 路由策略

Rotor 当前提供：

- `priority_weighted`：默认策略；优先级是硬边界，同一优先级内按权重选择。
- `fallback_order`：按确定顺序尝试候选 Channel。
- `weighted`：在兼容 Channel 之间按权重选择。
- `adaptive`：在同一优先级内结合成功率、延迟和当前负载动态评分。

运行时设置可通过 `GET/PUT /api/admin/settings` 查看和更新。自适应统计保存在进程
内，重启后重新学习；请求结果和路由决策会持久化。

## Control API、MCP 与 MindAgent

Control API 使用独立 Bearer Token，不接受普通的 `sk-` 用户 Token：

```bash
export ROTOR_CONTROL_API_TOKEN="$(
  python -c 'import secrets; print(secrets.token_urlsafe(32))'
)"
```

默认只读 scope：

```text
channel:read
request_trace:read
usage:read
```

`mcp/` 是独立的 `rotor-mcp` 包，当前通过 stdio 提供 Channel、请求 trace、近期失败
和模型用量查询。安装、启动和客户端配置见 [Rotor MCP README](mcp/README.md)。

管理页面中的 MindAgent 可以独立运行；只有配置 MCP 命令和 Control Token 后，才会
加载 Rotor MCP 诊断工具。

## 重要配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///~/.cache/rotor/rotor.db` | 主数据库 |
| `CONVERSATION_STORE_DIR` | `~/.cache/rotor/conversations` | 会话存储目录 |
| `LOG_LEVEL` | `INFO` | 日志级别；`DEBUG` 时开放 API 文档页面 |
| `ROTOR_CONTROL_API_TOKEN` | 未设置 | Control API 和 MCP 鉴权 |
| `ROTOR_CONTROL_API_SCOPES` | 三个只读 scope | Control API 权限 |
| `ROTOR_MINDAGENT_MCP_COMMAND` | 未设置 | MindAgent 启动独立 MCP Server 的命令 |

完整配置见[运行时配置](gitbook/operations/configuration.md)。

## 安全边界

- 当前 `/api/admin/*` 没有独立管理员鉴权，只应部署在受信任网络或受保护的反向代理
  后面。
- Provider API Key、Rotor 用户 Token、Control Token、数据库和会话文件都应视为
  敏感数据。
- Control API 与普通模型调用使用不同 Token，不应混用。
- Channel 能力探测可能产生真实上游调用和费用；普通模型列表探测只调用
  `GET /models`。

部署前请阅读[安全边界](gitbook/operations/security.md)。

## 开发与测试

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
pytest -q
```

MCP 测试：

```bash
cd mcp
python -m pip install -e ".[test]"
pytest -q
```

更多信息：

- [完整文档](gitbook/README.md)
- [安装与启动](gitbook/getting-started/installation.md)
- [协议兼容与转换](gitbook/concepts/protocols.md)
- [路由与故障转移](gitbook/concepts/routing-and-fallback.md)
- [日志、用量与数据存储](gitbook/operations/observability.md)
- [数据库与迁移](gitbook/development/database-migrations.md)
- [项目路线图](docs/roadmap.md)

## License

[MIT](LICENSE)
