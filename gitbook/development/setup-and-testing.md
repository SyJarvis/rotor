# 开发与测试

本页说明如何建立开发环境并运行不依赖真实上游的测试。

## 安装开发依赖

`requirements.txt` 包含 pytest、pytest-asyncio、OpenAI SDK 和 Anthropic SDK：

```bash
python -m pip install -r requirements.txt
python -m pip install -e .
```

## 运行测试

运行全部测试：

```bash
pytest
```

按功能运行：

```bash
pytest tests/test_routing.py
pytest tests/test_fallback.py
pytest tests/test_responses_protocol.py
pytest tests/test_anthropic_protocol.py
pytest tests/test_conversation_store.py
```

协议、路由、记账和会话测试大量使用模拟对象，不要求真实供应商凭据。以下文件则
包含面向真实或本地运行网关的调试/集成脚本，运行前应阅读其环境变量和清理行为：

```text
tests/test_gateway.py
tests/test_openai_sdk.py
tests/test_anthropic.py
tests/test_debug.py
```

## 代码入口

| 范围 | 位置 |
| --- | --- |
| FastAPI 应用 | `src/rotor/main.py` |
| 客户端和管理路由 | `src/rotor/api/` |
| 路由、fallback、记账 | `src/rotor/gateway/` |
| 协议与 provider adapter | `src/rotor/adapters/` |
| 持久化模型 | `src/rotor/models/` |
| 会话存储 | `src/rotor/conversations/` |
| 浏览器管理页 | `src/rotor/frontend/` |

修改协议行为时，应同时增加非流式、流式、工具调用、usage 和错误路径的回归测试。
修改路由行为时，应验证优先级边界、亲和、冷却和候选诊断。
