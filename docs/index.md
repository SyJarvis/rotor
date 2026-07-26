---
hide:
  - navigation
  - toc
---

# Rotor

:material-api: **API-only LLM 网关** · :material-translate: **兼容 OpenAI / Anthropic / Responses 协议** · :material-shield-check: **多渠道路由 + 鉴权 + 记账**

Rotor 是一个轻量级 LLM 网关,把多家上游 provider(智谱 / Moonshot / Kimi / MiniMax 等)统一暴露为:

- **OpenAI Chat Completions** — `POST /v1/chat/completions`
- **OpenAI Responses** — `POST /v1/responses`
- **OpenAI Images** — `POST /v1/images/generations`
- **Anthropic Messages** — `POST /anthropic/v1/messages`

通过浏览器管理页或 HTTP admin API 配置渠道、生成用户 Key、查看用量与日志。

---

## 特性

<div class="grid cards" markdown>

- :material-route:{ .lg .middle } **多渠道路由**

    ---

    按 **优先级 + 权重** 在多个渠道间负载均衡,失败时自动 fallback。

- :material-key:{ .lg .middle } **Token 鉴权与配额**

    ---

    为每个用户签发独立 API Key,设置用量配额,实时统计。

- :material-file-document-multiple:{ .lg .middle } **三协议兼容**

    ---

    一份渠道配置同时服务 OpenAI / Anthropic / Responses 客户端。

- :material-chart-line:{ .lg .middle } **用量与日志**

    ---

    请求级日志、Token 用量累计、内置管理页可视化。

- :material-lightning-bolt:{ .lg .middle } **流式优先**

    ---

    一等公民支持 SSE 流式响应,异步记账不阻塞响应。

- :material-database:{ .lg .middle } **轻量存储**

    ---

    默认 SQLite,可切 PostgreSQL;会话内容写文件系统。

</div>

---

## 快速开始

```bash
pip install -e .
rotor serve --host 0.0.0.0 --port 8000
```

打开管理页:

```text
http://localhost:8000
```

默认数据存放位置:

```text
~/.cache/rotor/
  rotor.db             # 渠道 / Token / 日志 / 用量
  conversations/       # 会话内容
```

继续阅读 → [安装与运行](./getting-started.md)

---

## 端点速查

| 路径 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 浏览器管理页 |
| `/v1/chat/completions` | POST | OpenAI Chat 协议 |
| `/v1/responses` | POST | OpenAI Responses 协议 |
| `/v1/images/generations` | POST | OpenAI Images 生图协议 |
| `/anthropic/v1/messages` | POST | Anthropic Messages 协议 |
| `/anthropic/v1/messages/count_tokens` | POST | Anthropic 输入 Token 计数 |
| `/v1/models` | GET | 已配置模型列表 |
| `/api/admin/channels` | GET/POST/PUT/DELETE | 渠道管理 |
| `/api/admin/tokens` | GET/POST/PUT/DELETE | Token 管理 |
| `/api/admin/logs` | GET | 请求日志 |
