#!/usr/bin/env python3
"""手动实现 Anthropic 兼容接口的流式调用（httpx 版本）"""

import asyncio
import httpx
import json
import os

API_KEY = os.getenv("GLM_API_KEY", "")
BASE_URL = "https://open.bigmodel.cn/api/anthropic".strip()  # ✅ 去除空格
API_ENDPOINT = f"{BASE_URL}/v1/messages"  # ✅ 完整路径
API_VERSION = "2023-06-01"
MODEL = "glm-4.7"

async def stream_anthropic_compatible(
    user_message: str,
    system_prompt: str = None,
    model: str = MODEL
):
    headers = {
        "x-api-key": API_KEY.strip(),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream"  # ✅ 关键：声明接收 SSE 流
    }

    # ✅ 注意：content 必须是数组格式，不是字符串！
    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": user_message}]
    }]
    
    payload = {
        "model": model,
        "max_tokens": 1000,
        "messages": messages,
        "stream": True  # ✅ 开启流式
    }
    
    if system_prompt:
        payload["system"] = system_prompt

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", API_ENDPOINT, headers=headers, json=payload) as response:
            response.raise_for_status()
            
            # ✅ 手动解析 SSE 流：每行以 "data: " 开头
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):  # 跳过心跳/注释行
                    continue
                if line.startswith("data: "):
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":  # 流结束标记
                        break
                    try:
                        chunk = json.loads(data_str)
                        # ✅ 手动模拟 SDK 的 chunk 类型判断
                        chunk_type = chunk.get("type")
                        
                        if chunk_type == "content_block_start":
                            print("\n" + "="*60)
                            print("Response Content:")
                            print("="*60)
                        
                        elif chunk_type == "content_block_delta":
                            delta = chunk.get("delta", {})
                            delta_type = delta.get("type")
                            
                            if delta_type == "thinking_delta":
                                thinking = delta.get("thinking", "")
                                if thinking:
                                    print(thinking, end="", flush=True)
                            elif delta_type == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    print(text, end="", flush=True)
                                    
                    except json.JSONDecodeError as e:
                        print(f"\n❌ JSON 解析失败: {e}, 原始数据: {data_str[:100]}")

async def main():
    print("🤖 开始流式调用 MiniMax (Anthropic 兼容接口)\n")
    await stream_anthropic_compatible(
        user_message="Hi, how are you?",
        system_prompt="You are a helpful assistant."
    )
    print("\n\n✅ 流式调用完成")

if __name__ == "__main__":
    asyncio.run(main())
