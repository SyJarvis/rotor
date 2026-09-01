# Rotor MCP Server

Rotor MCP Server 是 MindAgent 与 Rotor Backend 之间的 MCP 协议适配层。它只通过
Rotor Control API 读取数据，不导入 Rotor 后端模块，也不直接连接数据库。

## 当前实现

Server 使用官方 Python MCP SDK `2.0.0` 和 stdio transport，提供六个只读 Tool：

- `rotor_list_channels`：按启用状态、模型或协议查询当前安全渠道视图。
- `rotor_get_channel`：根据 Channel ID 查询当前安全渠道详情。
- `rotor_get_request_trace`：读取一次请求的路由尝试、渠道结果和归一化错误。
- `rotor_list_model_usage`：按请求模型汇总指定时间窗口内的 Token 用量，并按
  `total_tokens` 降序返回。
- `rotor_list_recent_failures`：按错误类别、模型、渠道或上游状态发现近期失败，
  并返回可继续查询的样本 Request ID。
- `rotor_evaluate_session_leases`：汇总 Session Lease 覆盖、迁移、缓存、费用和
  fallback 事实，并按 Channel、费率版本、峰谷时段和币种给出可比较 cohort。

六个 Tool 都提供严格的 JSON `outputSchema`，并声明：

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

Control API 默认不序列化 Provider `sanitized_body`，并在响应的
`meta.redactions` 中记录实际移除路径。MCP 仍保留同样的过滤逻辑，以兼容旧
Backend 或异常响应。Provider 错误信息仍属于不可信诊断数据，客户端不能将其作为
指令执行。

当前没有 Resource、Prompt、Sampling 或写 Tool；这些都是可选 MCP 能力，不影响
Codex、Claude Code 和其他 MCP Client 使用现有只读 Tool。

## 配置

```
mkdir -p ~/.cache/rotor
python -c 'import secrets; print("ROTOR_CONTROL_API_TOKEN=" + secrets.token_urlsafe(32))' > ~/.cache/rotor/control.env
chmod 600 ~/.cache/rotor/control.env

set -a
source ~/.cache/rotor/control.env
set +a
```

```bash
export ROTOR_CONTROL_API_URL=http://127.0.0.1:8000/api/control/v1
export ROTOR_CONTROL_API_TOKEN=replace-with-control-token
export ROTOR_AGENT_ID=external-agent
# 可选；建议每次 Agent Run 使用不同值
export ROTOR_AGENT_RUN_ID=run_123
```

`ROTOR_CONTROL_API_TOKEN` 是专用 Control API Token，不是普通的 `sk-` Rotor API
Key。Token 只通过 `Authorization: Bearer` 请求头发送，不会出现在 Tool 参数和错误
消息中。

也可以在 Rotor 管理页面的 **API Key → MCP Control Key** 创建 `rck_` 密钥。新密钥
创建后会立即被 Control API 识别；完整值只显示一次，请将页面提供的 MCP 环境变量复制到
启动 Codex、MindCode 或其他 MCP Client 的环境中。

## 运行

```bash
uv sync --project mcp
mcp/.venv/bin/rotor-mcp
```

stdio 的 stdout 只用于 MCP 消息。不要向 stdout 添加日志；需要诊断时应写入
stderr。

## 接入 Codex

先在启动 Codex 的 shell 中设置 Control API Token：

```bash
export ROTOR_CONTROL_API_TOKEN=replace-with-control-token
```

然后在 `~/.codex/config.toml`，或受信任项目的 `.codex/config.toml` 中添加：

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

重启 Codex 后检查 MCP Server 和 Tool 是否已加载。`env_vars` 让 Codex 从启动环境
传递 Token，避免把 Token 明文写入仓库配置。

## 接入 Claude Code

先在启动 Claude Code 的 shell 中设置 Control API Token：

```bash
export ROTOR_CONTROL_API_TOKEN=replace-with-control-token
```

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

CLI 方式会把展开后的环境变量写入 Claude Code 配置；不希望 Token 落盘时，使用上面
带环境变量替换的 `.mcp.json`。通过 `claude mcp get rotor` 或 Claude Code 内的
`/mcp` 检查连接状态。

Codex 和 Claude Code 的通用接入注意事项：

- `command` 使用绝对路径，避免不同 shell 的 PATH 不一致；
- Rotor Backend 必须已经启动，并配置相同的 `ROTOR_CONTROL_API_TOKEN`；
- 普通 `sk-` Rotor API Key 不能访问 Control API；
- 不要在静态配置中固定 `ROTOR_AGENT_RUN_ID`。只有启动包装器能为每次运行生成独立
  ID 时才设置它。

查询上海时区 2026-07-29 当天的模型用量：

```text
使用 Rotor MCP 查询 2026-07-29（Asia/Shanghai）调用的模型列表，
按消耗 Token 数降序排列。
```

Agent 应将自然日换算为明确的时间窗口：

```json
{
  "start_time": "2026-07-29T00:00:00+08:00",
  "end_time": "2026-07-30T00:00:00+08:00",
  "limit": 50
}
```

窗口为开始时间包含、结束时间不包含，最大跨度 7 天。未传时间时默认最近 1 小时。
`request_count` 按不同 Request ID 计数，`ledger_count` 是实际账本记录数；fallback
产生的多条账本会累计 Token，但不会重复增加请求数。

评估指定模型最近 24 小时的 Session Lease 事实：

```text
使用 Rotor MCP 评估 logical model `gpt-5.4` 最近 24 小时的 Session Lease，
先说明 blocking_reasons，再按 channel 和 tariff period 比较 cache 与成本事实。
不要据此修改路由设置。
```

`rotor_evaluate_session_leases` 默认查询最近 24 小时，最大跨度 30 天。返回的
`facts_complete_for_evaluation=true` 只表示 provider usage、usage schema v2、费用、
Session 和 Lease 事件等硬事实齐全；它不表示成本路由已经证明有收益，也不是启用建议。
聚合接口不返回 Session ID。跨币种窗口会用 `multiple_currencies` 阻止直接比较，应按
logical model、币种和更窄窗口重新查询。

## 接入 Rotor 内置 MindAgent

MindAgent 启动 MCP Server 时通过 `ROTOR_AGENT_RUN_ID` 绑定当前 Run。该字段不作为
Tool 参数暴露给模型，防止模型改写调用身份。Rotor Backend 负责最终鉴权，并会隐藏
同一 Agent Run 自身产生的诊断请求，避免递归诊断。

Rotor Backend 的 MindAgent MCP Client 是可选依赖，安装并指定独立 Server 命令：

```bash
uv sync --extra mcp
export ROTOR_CONTROL_API_TOKEN=replace-with-control-token
export ROTOR_MINDAGENT_MCP_COMMAND=/absolute/path/to/rotor/mcp/.venv/bin/rotor-mcp
```

Backend 会自动向 MCP 子进程传递 Control API URL、Token、Agent ID 和当前 Run ID。
未配置 `ROTOR_MINDAGENT_MCP_COMMAND` 时，管理端聊天保持原有的无 Rotor Tool 模式。

## 验证

运行离线测试：

```bash
uv sync --project mcp --extra test
mcp/.venv/bin/pytest -q mcp/tests
```

测试覆盖 Tool discovery、输入和输出 Schema、structured content、annotations、
Provider 正文移除、Control API 错误映射、MindAgent allowlist 以及真实 stdio
子进程连接。
