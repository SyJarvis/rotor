# MCP 诊断适配器

`rotor-mcp` 是独立的 Python 包和独立进程，通过 stdio 向 MCP Client 提供 Rotor 的只读
诊断数据。它只调用 [Control API](../reference/control-api.md)，不导入 Rotor 后端模块，也不直接
访问数据库。

Rotor Docker 镜像不包含、也不自动启动 MCP Server。

## 准备凭证

MCP Server 需要 Control API 的 Bearer 凭证，普通 `sk-` 用户 Key 不可用。两种方式：

- 在启动 MCP Server 的进程环境中设置 `ROTOR_CONTROL_API_TOKEN`；
- 在管理页“API Key → MCP Control Key”创建 `rck_` Key，并把页面给出的环境变量复制到
  客户端配置中。

固定 Token 可以用一次性命令生成，并保存到只允许本人读取的文件：

```bash
mkdir -p ~/.cache/rotor
python -c 'import secrets; print("ROTOR_CONTROL_API_TOKEN=" + secrets.token_urlsafe(32))' \
  > ~/.cache/rotor/control.env
chmod 600 ~/.cache/rotor/control.env

set -a
source ~/.cache/rotor/control.env
set +a
```

Rotor 服务端需要配置同一个 Token，否则 Control API 无法识别该凭证。

## 安装与运行

```bash
uv sync --project mcp
mcp/.venv/bin/rotor-mcp
```

环境变量：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ROTOR_CONTROL_API_URL` | `http://127.0.0.1:8000/api/control/v1` | Control API 基址，只接受 `http://` 或 `https://` |
| `ROTOR_CONTROL_API_TOKEN` | 未设置 | Control API Bearer 凭证 |
| `ROTOR_AGENT_ID` | `mindagent` | 审计中的操作者标识，最长 128 字符 |
| `ROTOR_AGENT_RUN_ID` | 未设置 | 单次 Agent Run 标识，用于排除自身产生的失败 |
| `ROTOR_CONTROL_API_TIMEOUT_SECONDS` | `20.0` | 单次 Control API 请求超时，范围 0–120 秒 |
| `ROTOR_CONTROL_API_MAX_RESPONSE_BYTES` | `1048576` | 响应体上限，范围 1 KB–10 MB |

项目模板通常显式设置 `ROTOR_AGENT_ID`，因此实际值以客户端配置为准。不要在静态配置中
固定 `ROTOR_AGENT_RUN_ID`，只有启动包装器能为每次运行生成独立 ID 时才设置它。

stdio 的 stdout 只承载 MCP 消息，诊断日志应写入 stderr。

## 提供的工具

六个工具都是只读、幂等、无外部副作用的查询：

| 工具 | 用途 | 需要的 scope |
| --- | --- | --- |
| `rotor_list_channels` | 按启用状态、模型或协议查询安全渠道视图 | `channel:read` |
| `rotor_get_channel` | 按 ID 查询渠道详情 | `channel:read` |
| `rotor_get_request_trace` | 读取一次请求的路由尝试与归一化错误 | `request_trace:read` |
| `rotor_list_recent_failures` | 按类别、模型、渠道或状态发现近期失败 | `request_trace:read` |
| `rotor_list_model_usage` | 汇总时间窗口内的模型 Token 用量 | `usage:read` |
| `rotor_evaluate_session_leases` | 汇总 Session Lease 覆盖、迁移、缓存与费用事实 | `usage:read` |

当前没有 Resource、Prompt、Sampling 或写 Tool。工具输出 schema 固定，并声明
`readOnlyHint: true`、`destructiveHint: false`、`idempotentHint: true`、
`openWorldHint: false`。

## 接入 Codex

先在启动 Codex 的 shell 中设置 Token：

```bash
export ROTOR_CONTROL_API_TOKEN=replace-with-control-token
```

然后在 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml` 中添加：

```toml
[mcp_servers.rotor]
command = "/absolute/path/to/rotor/mcp/.venv/bin/rotor-mcp"
cwd = "/absolute/path/to/rotor/mcp"
required = true
startup_timeout_sec = 10
tool_timeout_sec = 30
enabled_tools = [
  "rotor_evaluate_session_leases",
  "rotor_get_channel",
  "rotor_get_request_trace",
  "rotor_list_channels",
  "rotor_list_model_usage",
  "rotor_list_recent_failures",
]
env_vars = ["ROTOR_CONTROL_API_TOKEN"]

[mcp_servers.rotor.env]
ROTOR_CONTROL_API_URL = "http://127.0.0.1:8000/api/control/v1"
ROTOR_AGENT_ID = "codex"
```

`env_vars` 让 Codex 从启动环境传入 Token，避免把明文写进配置。`command` 使用绝对路径，
避免不同 shell 的 `PATH` 不一致。

## 接入 Claude Code

项目级接入可在仓库根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "rotor": {
      "type": "stdio",
      "command": "/absolute/path/to/rotor/mcp/.venv/bin/rotor-mcp",
      "args": [],
      "env": {
        "ROTOR_CONTROL_API_URL": "http://127.0.0.1:8000/api/control/v1",
        "ROTOR_CONTROL_API_TOKEN": "${ROTOR_CONTROL_API_TOKEN}",
        "ROTOR_AGENT_ID": "claude-code"
      }
    }
  }
}
```

也可以用 CLI 添加本地配置：

```bash
claude mcp add --transport stdio --scope local \
  --env ROTOR_CONTROL_API_URL=http://127.0.0.1:8000/api/control/v1 \
  --env ROTOR_CONTROL_API_TOKEN="$ROTOR_CONTROL_API_TOKEN" \
  --env ROTOR_AGENT_ID=claude-code \
  rotor -- /absolute/path/to/rotor/mcp/.venv/bin/rotor-mcp
```

CLI 方式会把展开后的环境变量写入 Claude Code 配置；不希望 Token 落盘时使用上面的
`${ROTOR_CONTROL_API_TOKEN}` 写法。用 `claude mcp get rotor` 或 `/mcp` 检查连接状态。

## 在 Agent 中使用

查询某个自然日的模型用量：

```text
使用 Rotor MCP 查询 2026-07-29（Asia/Shanghai）调用的模型列表，
按消耗 Token 数降序排列。
```

Agent 应把自然日换算为明确窗口，开始包含、结束不包含：

```json
{
  "start_time": "2026-07-29T00:00:00+08:00",
  "end_time": "2026-07-30T00:00:00+08:00",
  "limit": 50
}
```

模型用量与近期失败窗口最大跨度 7 天，未传时间时默认最近 1 小时；Session Lease 评估
默认最近 24 小时，最大跨度 30 天。

MCP 的 `rotor_list_recent_failures` 单页 `limit` 上限是 100，而 Control API 本身允许到
200。

评估 Session Lease 事实：

```text
使用 Rotor MCP 评估 logical model `gpt-5.4` 最近 24 小时的 Session Lease，
先说明 blocking_reasons，再按 channel 和 tariff period 比较 cache 与成本事实。
不要据此修改路由设置。
```

## 排查

| 现象 | 检查 |
| --- | --- |
| 工具未加载 | `command` 是否为绝对路径；Server 是否因缺少 Token 退出 |
| `401 control_invalid_credential` | 是否误用了 `sk-` 用户 Key |
| `503 control_api_not_configured` | Rotor 是否设置了 `ROTOR_CONTROL_API_TOKEN` |
| `403 control_scope_required` | `rck_` Key 的 scope 或被覆盖的 `ROTOR_CONTROL_API_SCOPES` |
| 连接超时 | `ROTOR_CONTROL_API_URL` 是否指向正在运行的 Rotor |

Control API 的完整接口见[Control API](../reference/control-api.md)。
