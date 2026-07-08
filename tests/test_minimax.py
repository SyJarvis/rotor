#!/usr/bin/env python3
"""Test AI Provider Anthropic-compatible APIs."""

import asyncio
import httpx
import json
import os


# ========== 配置区域 ==========

# Anthropic 配置
ANTHROPIC_CONFIG = {
    "name": "Anthropic",
    "base_url": "https://api.anthropic.com",
    "api_key": "your-anthropic-api-key-here",  # 请替换为你的Anthropic API密钥
    "model": "claude-3-sonnet-20240229",
}

# MiniMax 配置 (Anthropic兼容)
MINIMAX_CONFIG = {
    "name": "MiniMax",
    "base_url": "https://api.minimaxi.com/anthropic",  # 正确：使用Anthropic兼容端点
    "api_key": os.getenv("MINIMAX_API_KEY", ""),
    "model": "MiniMax-M2.5",  # 使用文档中支持的模型
}

# 智谱 AI 配置 (使用OpenAI兼容API)
ZHIPU_CONFIG = {
    "name": "智谱 AI",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",  # 改用OpenAI兼容API
    "api_key": os.getenv("GLM_API_KEY", ""),  # 注意：智谱AI使用Bearer token
    "model": "glm-4",  # 修正：使用正确的模型名称
}


#!/usr/bin/env python3
"""Test AI Provider Anthropic-compatible APIs."""

import asyncio
import httpx
import json
# ========== 配置区域 ==========
PROVIDER_CONFIG = {
    "name": "MiniMax",  # 可切换: Anthropic / MiniMax / ZhipU
    "base_url": "https://open.bigmodel.cn/api/anthropic",  # ✅ 去除末尾空格
    "api_key": os.getenv("GLM_API_KEY", ""),
    "model": "glm-4.7",
    "api_version": "2023-06-01",
    "use_bearer_auth": False,  # MiniMax Anthropic兼容接口通常用 x-api-key，OpenAI兼容接口用 Bearer
}

MAX_TOKENS = 1024
TIMEOUT = 30.0


async def call_anthropic_compatible_api(
    user_message: str,
    system_prompt: str = None,
    config: dict = None
):
    """
    异步调用 Anthropic 兼容 API（支持 MiniMax / Anthropic / 其他兼容接口）
    """
    cfg = config or PROVIDER_CONFIG
    api_key = cfg.get("api_key", "").strip()
    base_url = cfg.get("base_url", "").strip()
    model = cfg.get("model", "claude-3-sonnet-20240229")
    api_version = cfg.get("api_version", "2023-06-01")
    use_bearer = cfg.get("use_bearer_auth", False)

    if not api_key:
        raise ValueError("API Key 未配置，请检查 PROVIDER_CONFIG")

    # 构建 Headers（根据厂商兼容性调整）
    headers = {
        "content-type": "application/json"
    }
    if use_bearer:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = api_version

    # 构建消息体
    messages = [{"role": "user", "content": user_message}]
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": messages
    }

    # 系统提示：部分兼容接口不支持 system 字段，可降级放入 messages
    if system_prompt:
        # 先尝试标准 system 字段
        payload["system"] = system_prompt
        # 如果报错，可改为：messages.insert(0, {"role": "system", "content": system_prompt})

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            print(f"📤 请求 URL: {base_url}")
            print(f"📦 请求体: {json.dumps(payload, ensure_ascii=False, indent=2)}")
            
            response = await client.post(base_url, headers=headers, json=payload)
            
            # 打印原始响应便于调试
            print(f"📥 状态码: {response.status_code}")
            print(f"📄 响应头: {dict(response.headers)}")
            print(f"📃 响应体: {response.text[:500]}...")  # 只打印前500字符

            response.raise_for_status()
            result = response.json()
            
            # 兼容不同返回结构
            if "content" in result and isinstance(result["content"], list):
                return result["content"][0].get("text", "")
            elif "choices" in result:  # OpenAI 格式 fallback
                return result["choices"][0]["message"]["content"]
            else:
                return json.dumps(result, ensure_ascii=False)
                
        except httpx.HTTPStatusError as e:
            print(f"❌ HTTP 错误: {e.response.status_code}")
            print(f"📄 错误详情: {e.response.text}")
            raise RuntimeError(f"HTTP {e.response.status_code}: {e.response.text}")
        except json.JSONDecodeError as e:
            print(f"❌ JSON 解析失败: {e}")
            print(f"📄 原始响应: {response.text}")
            raise RuntimeError(f"JSON 解析错误: {e}")
        except httpx.RequestError as e:
            print(f"❌ 请求异常: {type(e).__name__} - {str(e)}")
            raise RuntimeError(f"网络请求失败: {str(e)}")
        except Exception as e:
            print(f"❌ 未知错误: {type(e).__name__} - {str(e)}")
            raise


async def main():
    user_input = "请简要介绍量子计算的基本原理。"
    system_instruction = "你是一位专业的科技助手，请用简洁清晰的语言回答。"

    try:
        answer = await call_anthropic_compatible_api(
            user_message=user_input,
            system_prompt=system_instruction,
            config=PROVIDER_CONFIG
        )
        print("\n✅ 模型回答：\n", answer)
    except Exception as e:
        print(f"\n💥 调用失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
