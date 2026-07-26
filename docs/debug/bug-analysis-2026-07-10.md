# Rotor Bug 分析报告

- 报告日期：2026-07-10
- 分析基线：`main` 分支（最近提交 `1abba4f update v1`）
- 代码量：约 2,700 行 Python + 前端管理页
- 分析范围：src/rotor 全量 + tests + docs/debug/log

## 摘要

本次共识别 8 个问题，按严重度分级：

| 编号 | 标题 | 严重度 | 状态 | 复检（2026-07-10） |
|---|---|---|---|---|
| BUG-001 | `openai_to_anthropic_response` 引用未定义变量 `function` | **P0 阻断** | 生产已复现 | ✅ 已修复，测试覆盖 |
| BUG-002 | 流式响应在首字节之后失败时无法切换渠道 | **P1 正确性** | 代码确认 | ⚠️ **未修复**（仅 `mark_unavailable` 标 cooldown，仍无渠道切换） |
| BUG-003 | 流式 usage 缺失时静默记为 0 | **P1 记账** | 代码确认 | ✅ 已修复，测试覆盖 |
| BUG-004 | Moonshot 适配器在 `channel.extra=None` 时崩溃 | P2 健壮性 | 代码确认 | ✅ 已修复，测试覆盖 |
| BUG-005 | `AnthropicBedrockAdapter` 已定义但未注册 | P2 死代码 | 代码确认 | 未处理 |
| BUG-006 | 多 worker 下路由状态不共享 | P2 部署 | 架构限制 | 未处理 |
| BUG-007 | Alembic 缺少初始迁移文件 | P2 运维 | 代码确认 | 未处理 |
| BUG-008 | `AsyncClient` 每请求新建，连接池未复用 | P3 性能 | 代码确认 | 未处理 |

**复检环境**：`conda activate toolchains` + `pip install -e .`（editable），20/20 相关测试通过。

## 复检总结（2026-07-10）

### BUG-001 ✅ 已修复

**修复点**：`src/rotor/api/v1/anthropic.py:175-237`

错位的 `arguments` 解析代码已从 `if content:` 分支移除，正确放入 `for tool_call in tool_calls:` 循环内。每个 tool_call 独立解析自己的 `function.arguments`，不再共用错配的 `tool_input`。

**直接调用验证**（在 toolchains 环境中）：

```
text-only: [{'type': 'text', 'text': 'hello'}] {'input_tokens': 3, 'output_tokens': 2}
tool_calls: [('A', {'x': 1}), ('B', {'y': 2})]
```

- 纯文本响应（原崩溃路径）不再 `UnboundLocalError`。
- 多 tool_calls 各自 `input` 独立正确。

**测试覆盖**（`tests/test_anthropic_protocol.py`）：

- `test_openai_text_response_converts_to_anthropic_message`（行 68）
- `test_openai_tool_calls_convert_to_anthropic_tool_use_blocks`（行 91）— 覆盖字符串和 dict 两种 arguments 形态、多个 tool_call
- `test_empty_openai_response_converts_to_empty_anthropic_text_block`（行 140）

### BUG-003 ✅ 已修复

**修复点**：

1. `src/rotor/gateway/accounting.py:143-158` 新增 `streaming_usage(prompt_tokens, completion_tokens, has_provider_usage)`。当 `has_provider_usage=False` 时直接返回 `UsageData(usage_source="missing")`，token 字段全部为 0；为 True 时走原有 `extract_usage`。
2. `src/rotor/api/v1/chat.py:262, 277-278, 295-299`：流式 generator 内新增 `has_provider_usage` 跟踪变量，仅当 chunk 真的带 `usage` 字段时置 True，最后调用 `streaming_usage`。
3. `src/rotor/api/v1/anthropic.py:543, 569-570, 588-592`：Anthropic 流式路径同样接入。

**测试覆盖**：

- `test_streaming_usage_missing_when_provider_sends_no_usage`（行 209）
- `test_streaming_usage_provider_when_usage_chunk_is_seen`（行 220）

**遗留观察**：`responses.py` 路径未接入 `streaming_usage`（它直接复用 `chat.py` 的 `_handle_streaming_request`，会继承修复，但 responses 自己的代码路径如果将来独立出来需要补）。

### BUG-004 ✅ 已修复

**修复点**：`src/rotor/adapters/providers/moonshot.py:60`

```python
# 修复前
if "enable_search" in self.channel.extra:
# 修复后
if self.channel.extra and "enable_search" in self.channel.extra:
```

与 `zhipu.py:53` / `minimax.py:53` 的守卫风格一致。

**测试覆盖**：`test_moonshot_adapter_allows_missing_extra_config`（行 558）— `channel.extra=None` 不再抛 `TypeError`。

### BUG-002 ⚠️ 未修复（确认仍存在）

**复检路径**：重新通读 `chat.py:103-111`、`anthropic.py:402-410`、`adapters/base.py:117-131`。

`make_request` 仍只对握手期错误 `raise_for_status`；流式 generator 内 `except` 仍只做 `mark_unavailable` + 发 SSE error 事件，**没有**调用下一个候选渠道。

```python
# chat.py:324-354（修复前后一致）
except Exception as e:
    logger.error(f"Streaming error: {e}")
    if should_fallback(e):
        routing_engine.mark_unavailable(request.model, channel)   # 只标 cooldown
    ...
    yield f"data: {_json_dumps(error_chunk)}\n\n"                  # 发 error 就结束
```

外层 `for attempt, channel in enumerate(candidates)` 循环在 `return result`（行 111）时已退出，generator 抛错时无法再回到循环。

**这是设计层面的限制**，报告原"修复建议"里的方案（首 chunk 探测 / 已 yield 字节计数器）都需要较大改动，未实施。

**当前行为**：

- 握手期失败（HTTP 非 2xx、连接超时）：能 fallback 到下一渠道。
- 流中途失败（首字节已发出）：客户端收到不完整响应 + SSE error 事件，本次请求不切换；但渠道会被 cooldown，下次请求走别的渠道。

**建议**：如需真正修流式 fallback，应作为一个独立任务规划，参考报告中"中期方案"。

### 测试结果

```
$ conda activate toolchains && pip install -e . && pytest tests/test_anthropic_protocol.py tests/test_fallback.py
============================== 20 passed in 0.21s ==============================
```

全量测试 37 passed / 7 failed，失败全部是既有的 async 测试缺 `@pytest.mark.asyncio` marker（`test_anthropic.py`、`test_debug.py`、`test_openai_sdk.py`），与本次修复无关。

---

## BUG-001（P0 阻断）`openai_to_anthropic_response` 引用未定义变量

### 现场证据

`docs/debug/log` 已经在生产环境抓到完整堆栈：

```
POST /anthropic/v1/messages?beta=true HTTP/1.1" 500 Internal Server Error
  File ".../rotor/api/v1/anthropic.py", line 494, in _handle_non_streaming_request
    anthropic_response = openai_to_anthropic_response(response_data, anthropic_request.model)
  File ".../rotor/api/v1/anthropic.py", line 188, in openai_to_anthropic_response
    arguments = function.get("arguments", "{}")
UnboundLocalError: cannot access local variable 'function' where it is not associated with a value
```

### 触发条件

满足以下三条即必现 500：

1. 入口走 `/anthropic/v1/messages` 非流式；
2. 路由命中的 channel 是 OpenAI 兼容上游（如 zhipu、moonshot、minimax、kimi、openai、deepseek），返回的 `choices[0].message.content` 非空；
3. 不走流式（`stream=False` 或缺省）。

也就是说：**只要 Anthropic SDK 客户端打到 OpenAI 兼容渠道且拿到一段普通文本回复，就会崩**。这是当前最常用的接入路径之一。

### 根本原因

`src/rotor/api/v1/anthropic.py:175-213`：

```python
def openai_to_anthropic_response(openai_response: dict, model: str) -> dict:
    ...
    content = message.get("content", "")
    tool_calls = message.get("tool_calls") or []

    content_blocks = []
    if content:
        arguments = function.get("arguments", "{}")        # ← function 未定义
        try:
            tool_input = (
                arguments if isinstance(arguments, dict)
                else json.loads(arguments or "{}")
            )
        except (json.JSONDecodeError, TypeError):
            logger.warning(...)
            tool_input = {}
        content_blocks.append({"type": "text", "text": content})

    for tool_call in tool_calls:
        import json
        function = tool_call.get("function", {})            # ← 才在这里赋值
        content_blocks.append({
            "type": "tool_use",
            "id": tool_call.get("id", ""),
            "name": function.get("name", ""),
            "input": tool_input,                            # ← 还会错配 tool_input
        })
```

两个问题叠加：

1. `if content:` 分支里整段 `arguments/tool_input` 解析属于**错位粘贴**——这段代码本应在 `for tool_call in tool_calls:` 循环内部，用于把 OpenAI 的字符串型 `function.arguments` 解析成 dict 写入 Anthropic `tool_use.input`。
2. 真正进到 `for tool_call` 循环时，`tool_input` 引用的是上文错位解析出的值（若 content 非空），或未定义（若 content 为空）——后者会再抛一次 `UnboundLocalError`。

### 影响

- 非流式 Anthropic → OpenAI 兼容渠道路径**完全不可用**，客户端拿 500。
- 流式路径不走这个函数，不受影响。
- `tool_use` 返回时，所有 tool_call 共用同一个错配的 `tool_input`，即使修了 NameError 也会写错数据。

### 修复建议

把 `arguments` 解析逻辑移到 `for tool_call` 循环内，移除 `if content:` 里的错位代码：

```python
content_blocks = []
if content:
    content_blocks.append({"type": "text", "text": content})

for tool_call in tool_calls:
    function = tool_call.get("function", {})
    arguments = function.get("arguments", "{}")
    try:
        tool_input = (
            arguments if isinstance(arguments, dict)
            else json.loads(arguments or "{}")
        )
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid tool arguments from upstream: %r", arguments)
        tool_input = {}
    content_blocks.append({
        "type": "tool_use",
        "id": tool_call.get("id", ""),
        "name": function.get("name", ""),
        "input": tool_input,
    })
```

顺带删掉函数顶上那行无用的 `import datetime`（177 行）。

### 测试建议

补一个直接覆盖 `openai_to_anthropic_response` 的单测，至少三个 case：

- 纯文本响应（覆盖崩溃路径）；
- 带 tool_calls 的响应（覆盖 input 解析）；
- 空响应（覆盖 `content_blocks` 为空时的兜底）。

现有 `tests/test_anthropic_protocol.py` 完全没有测这个函数，是漏检的直接原因。

---

## BUG-002（P1 正确性）流式响应在首字节之后失败时无法切换渠道

### 触发条件

1. 客户端请求 `stream=True`；
2. 上游返回 HTTP 200，开始正常推 SSE；
3. 流到一半上游断连 / 抛错 / 超时。

### 根本原因

`src/rotor/api/v1/chat.py:103-111`（anthropic/responses 路径结构相同）：

```python
if request.stream:
    response = await adapter.make_request(request)        # 已 raise_for_status 通过
    result = await _handle_streaming_request(...)         # 仅返回 StreamingResponse
    set_routing_headers(result, channel, request.model, attempt > 0)
    return result                                         # ← HTTP 200 已对客户端发出
```

进入 `_handle_streaming_request` 后，`StreamingResponse` 把真正的生成器推迟到响应体阶段执行。`for attempt, channel in enumerate(candidates)` 循环在 `return result` 时已经退出，**后续候选 channel 不会被尝试**。

`adapters/base.py:117-131` 的 `make_request`：

```python
if request.stream:
    http_request = ...
    response = await self.http_client.send(http_request, stream=True)
    try:
        response.raise_for_status()                       # 只能抓到握手期错误
    except Exception:
        await response.aclose()
        raise
    return response
```

`raise_for_status()` 只能拦截握手期（HTTP status ≠ 2xx）。一旦 200 + SSE 头已发出，后续错误只能在生成器里 `except`，但那时已无法切渠道：

`chat.py:323-353`：

```python
except Exception as e:
    logger.error(f"Streaming error: {e}")
    if should_fallback(e):
        routing_engine.mark_unavailable(request.model, channel)   # 只标 cooldown
    ...
    yield f"data: {_json_dumps(error_chunk)}\n\n"                  # 给客户端发 error 事件就结束
```

### 影响

- 客户端看到不完整响应（已经渲染了一部分文本然后突然 error 事件结束）。
- 即使配了 fallback 渠道，本次请求也不会被救回，只能在下一次请求时切换。
- 对长输出 / 长上下文场景很不友好——前面几百 token 都正确，最后几 token 错了无法续。

### 修复建议

短期（不重构）：

- 在流式 generator 内部首次异常时，如果尚未 yield 任何有效 chunk，**重新抛出**让外层 fallback；如果已 yield，则只能发 error 事件（保持现状）。
- 用一个"已发字节计数器"判断是否安全重试。

中期（推荐）：

- 在 `make_request` 内对 streaming 做"首 chunk 探测"——读取第一个 chunk 后再决定是 raise 还是把已读 chunk 接回去交给 generator。这样能区分"握手失败"（可 fallback）和"流中断"（不可 fallback）。

### 测试建议

- mock 一个上游：先发 1 个正常 chunk，再抛 `httpx.RemoteProtocolError`。断言：客户端收到 error 事件，UsageLedger 有失败记录，channel 被 cooldown。
- mock 上游：握手就 503。断言：客户端从 fallback 渠道拿到完整响应。

---

## BUG-003（P1 记账）流式 usage 缺失时静默记为 0

### 触发条件

任意流式请求，且上游不在最后一个 chunk 里带 `usage` 字段。

### 根本原因

`src/rotor/api/v1/chat.py:259-298`：

```python
prompt_tokens = 0
completion_tokens = 0
...
async for chunk in adapter.stream_convert_response(response, request):
    ...
    usage = chunk.get("usage") or {}
    prompt_tokens = max(prompt_tokens, usage.get("prompt_tokens", 0))
    completion_tokens = max(completion_tokens, usage.get("completion_tokens", 0))
...
usage_data = accounting_service.extract_usage({
    "usage": {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
})
await accounting_service.record_success(..., usage=usage_data, ...)
```

`extract_usage`（`gateway/accounting.py:125-141`）：

```python
return UsageData(
    prompt_tokens=prompt_tokens,
    completion_tokens=completion_tokens,
    total_tokens=total_tokens,
    ...
    usage_source="provider" if usage else "missing",   # ← 这里 usage 非空
)
```

由于外层强行把 0 塞进 `{"usage": {...}}` 再调 `extract_usage`，`usage` 永远非空，`usage_source` 永远是 `"provider"`。**表面上记账成功，实际上 token 全是 0**。

### 影响

- 调用方不发送 `stream_options.include_usage` 或上游不支持时（部分国产 OpenAI 兼容服务不发 usage chunk），`UsageLedger` 和 `RequestLog` 会记录 `total_tokens=0`。
- Token 配额不会扣减，`token.used_quota` 不增长——配额限制形同虚设。
- 报表里该渠道看起来"零成本"，与真实消耗不符。
- `docs/基准测试.md` 第 4.3 节明确要求 `SDK total_tokens = RequestLog.total_tokens = UsageLedger.total_tokens = used_quota 增量`，此 bug 直接破坏这条不变式。

### 修复建议

区分两种来源：

```python
detected_usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
if prompt_tokens == 0 and completion_tokens == 0:
    usage_data = UsageData(usage_source="missing")
else:
    usage_data = accounting_service.extract_usage({"usage": detected_usage})
```

或在 `extract_usage` 里加一个旁路参数，让调用方显式声明"这是估计/缺失"。

更彻底的方案：对支持 OpenAI 协议的上游强制带 `stream_options.include_usage=True`（`OpenAICompatibleAdapter.prepare_request_body` 已经这么做了，但只对**直接 OpenAI 协议入口**生效；从 Anthropic 入口进来的请求不会走这个分支，因为内部 `ChatCompletionRequest` 已被 provider 自己的 `convert_request` 处理过）。

### 测试建议

- mock 上游：发 3 个不带 usage 的 chunk + `[DONE]`。断言：`UsageLedger.usage_source == "missing"`，`token.used_quota` 不变。
- mock 上游：最后一个 chunk 带 usage。断言：记账与 chunk 中的 usage 一致。

---

## BUG-004（P2 健壮性）Moonshot 适配器在 `channel.extra=None` 时崩溃

### 根本原因

`src/rotor/adapters/providers/moonshot.py:60-62`：

```python
# Moonshot-specific parameters from extra config
if "enable_search" in self.channel.extra:        # ← extra 可能为 None
    body["enable_search"] = self.channel.extra["enable_search"]
```

`Channel.extra`（`models/channel.py:59`）声明为 `JSON default=dict`，但列允许 NULL。从老库或直接 SQL 写入的渠道可能为 `None`。

对比 `zhipu.py:53` / `minimax.py:53` 都有 `if self.channel.extra:` 守卫，moonshot 漏了。

### 修复建议

改成 `if self.channel.extra and "enable_search" in self.channel.extra:`，或更彻底地把 `Channel.extra` 的 SQLAlchemy 列改为 `default=dict, nullable=False`。

### 测试建议

加一个 `channel.extra=None` 的 fixture，跑 `convert_request` 不抛异常。

---

## BUG-005（P2 死代码）`AnthropicBedrockAdapter` 已定义但未注册

### 根本原因

`src/rotor/adapters/protocol/anthropic.py:85-101` 定义了 `AnthropicBedrockAdapter`：

```python
class AnthropicBedrockAdapter(AnthropicAdapter):
    """Adapter for Anthropic via AWS Bedrock."""
    async def get_request_url(self, request):
        ...
    def setup_request_headers(self, request):
        # 注释自承"需要 AWS 签名，这里只是简化版"
        return {"Content-Type": "application/json"}
```

但 `adapters/factory.py:18-27` 的注册表里**没有 `"bedrock"` 项**：

```python
_adapters: dict[str, Type[BaseAdapter]] = {
    "openai": OpenAIAdapter,
    "deepseek": OpenAIAdapter,
    "azure": AzureOpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "moonshot": MoonshotAdapter,
    "minimax": MiniMaxAdapter,
    "zhipu": ZhipuAdapter,
    "kimi": KimiAdapter,
}
```

### 影响

- 配 `type="bedrock"` 的 channel 会在 `AdapterFactory.create_adapter` 抛 `ValueError: Unsupported provider type`。
- 现有实现即使能注册也跑不通：Bedrock 需要 AWS Signature V4，当前只返回 `{"Content-Type": "application/json"}`，无法通过鉴权。

### 修复建议

二选一：

- **删掉**：明确不支持 Bedrock，避免维护负担。
- **补全**：引入 `boto3` 或手写 SigV4，在 factory 注册 `"bedrock": AnthropicBedrockAdapter`，并在 `channels/presets.py` 加 preset。

短期建议删除，等真有 Bedrock 需求再做完整实现。

---

## BUG-006（P2 部署）多 worker 下路由状态不共享

### 根本原因

`src/rotor/gateway/routing.py:146-148`：

```python
routing_engine = RoutingEngine(
    strategy=application_settings.get().routing.strategy,
)
```

模块级单例。`_cooldowns: dict[tuple[str, int], float]` 存在进程内存（`routing.py:24`）。

uvicorn 默认单进程；但生产部署常用 `--workers >1` 或多实例水平扩展。每个 worker：

- 独立维护 cooldown——某 worker 把 channel A 标 unavailable，其他 worker 仍会往 A 发请求。
- 独立计算 affinity 排序——同一 conversation 在不同 worker 上可能落到不同 channel，破坏"会话粘性"。
- `application_settings.save()` 只写本地文件 + 更新本进程的 `routing_engine.strategy`，其他 worker 仍是旧 strategy。

### 影响

- 故障渠道在部分 worker 上仍然吃流量，fallback 行为不一致。
- 多 worker 部署下 affinity 几乎失效。
- 改 routing strategy 后只有处理该 PUT 请求的那个 worker 生效。

### 修复建议

短期：文档明确"目前只支持单 worker"，`scripts/launch_serve.py` 不允许多 worker。

中期：把 cooldown / affinity 状态外置到 Redis 或 DB；`application_settings` 改成基于文件 mtime 轮询或广播。

---

## BUG-007（P2 运维）Alembic 缺少初始迁移文件

### 根本原因

`alembic/env.py` 已配好读 `rotor.config.settings.DATABASE_URL` 和 `Base.metadata`，但 `alembic/versions/` 目录不存在，没有任何迁移脚本。

当前建表走 `database.py:36-39`：

```python
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

`create_all` 只能新建**不存在的表**，不会修改已有表的 schema。

### 影响

- 现网 SQLite/PG 库一旦建过表，后续给 `UsageLedger` 加列（已经加过多轮：`cached_tokens`/`reasoning_tokens`/`input_audio_tokens`/`output_audio_tokens`/`provider_model`/`provider_protocol` 等）不会自动生效。
- 老部署升级后写入新字段会抛 `column "xxx" does not exist`（SQLite）或类似错误。
- `docs/基准测试.md` 要求在 SQLite 和 PostgreSQL 上都验证——目前两边都缺迁移路径。

### 修复建议

1. 生成初始迁移：`alembic revision --autogenerate -m "init schema"`。
2. 把 `init_db` 改为可选——生产部署应走 `alembic upgrade head`，开发态才用 `create_all`。
3. `cli.py` 加一个 `rotor migrate` 子命令包 `alembic upgrade head`。

---

## BUG-008（P3 性能）`AsyncClient` 每请求新建，连接池未复用

### 根本原因

每个请求路径都各自 `AsyncClient(timeout=120.0)`：

- `api/v1/chat.py:83`
- `api/v1/anthropic.py:395`
- `api/v1/responses.py:207`

非流式在 finally 里 `aclose()`；流式在 generator finally 里 `aclose()`。

httpx 的 `AsyncClient` 内部维护 keep-alive 连接池。每次 new + close 等于每次都做 TCP/TLS 握手，对 HTTPS 上游尤其贵（~100ms 起）。

### 影响

- `docs/基准测试.md` 关心的"Rotor 相比直连增加多少延迟"指标直接被这个 bug 抬高。P95 额外开销里相当一部分是握手。
- 高并发下 ephemeral port 占用快，可能触发 `Cannot assign requested address`。

### 修复建议

把 `AsyncClient` 提到 FastAPI `lifespan` 里全局创建，存到 `app.state.http_client`，依赖注入取出。设置合理的 `limits=httpx.Limits(max_keepalive_connections=50, max_connections=200)`。

`make_request` 接受注入的 client，请求处理函数不再各自构造。

---

## 优先级与建议执行顺序

| 阶段 | 修什么 | 理由 |
|---|---|---|
| 立即（阻断） | BUG-001 | 生产已崩，最常见的接入路径走不通 |
| 本周 | BUG-003、BUG-007 | 记账正确性 + schema 升级路径，影响所有部署 |
| 本周 | BUG-004、BUG-005 | 健壮性清理，改动小 |
| 下个迭代 | BUG-002 | 流式 fallback 体验，需要设计 |
| 下个迭代 | BUG-008 | 性能优化，依赖基准测试数据驱动 |
| 视部署形态 | BUG-006 | 单 worker 部署可暂缓 |

## 测试覆盖缺口

当前 `tests/test_anthropic_protocol.py` 共 12 个 case，全部聚焦于：

- 入站请求转换（`anthropic_to_openai_request`）
- Anthropic 原生字段保留（`AnthropicAdapter.convert_request`）
- 流式事件序列（`OpenAIToAnthropicStreamConverter`）
- 出站协议转换（`ProtocolConverter.anthropic_stream_to_openai`）

**完全没覆盖**：

- `openai_to_anthropic_response` 非流式响应转换——直接导致 BUG-001 上线。
- 流式 generator 内的异常路径——BUG-002 / BUG-003 都在这里。
- 多渠道 fallback 在真实 HTTP 路径上的端到端验证（`test_fallback.py` 只测了 `should_fallback` 纯函数）。
- 记账一致性（SDK usage ↔ RequestLog ↔ UsageLedger ↔ token.used_quota）——`docs/基准测试.md` 第 4.3 节的核心不变式没有任何测试守护。

建议把这些列为测试补齐的优先项，再开始下一轮重构。
