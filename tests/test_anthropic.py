#!/usr/bin/env python3
"""
测试 Anthropic 协议端点

直接使用 httpx 测试，不使用 Anthropic SDK
"""
import asyncio
import httpx
import json

GATEWAY_URL = "http://localhost:8000"


async def test_anthropic_endpoint():
    """测试 Anthropic 端点"""
    # 1. 生成 token
    print("=" * 60)
    print("生成 API Key...")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{GATEWAY_URL}/api/admin/tokens/generate", params={"name": "anthropic-test"})
        if resp.status_code not in (200, 201):
            print(f"❌ 生成 token 失败: {resp.status_code} - {resp.text}")
            return

        token_data = resp.json()
        token = token_data.get("key")
        print(f"✅ Token: {token[:20]}...")

        # 2. 测试 Anthropic 端点
        print("\n" + "=" * 60)
        print("测试 Anthropic 协议端点")
        print("=" * 60)

        headers = {
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

        # 测试请求
        payload = {
            "model": "glm-4.7",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "Hello, how are you?"}
            ]
        }

        print(f"\n📤 发送请求到: {GATEWAY_URL}/anthropic/v1/messages")
        print(f"Payload: {json.dumps(payload, indent=2)}")

        resp = await client.post(f"{GATEWAY_URL}/anthropic/v1/messages", headers=headers, json=payload)

        print(f"\n📥 响应状态: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            print(f"\n✅ 响应成功!")
            print(f"响应内容: {json.dumps(data, indent=2, ensure_ascii=False)}")

            # 提取内容
            if "content" in data and isinstance(data["content"], list):
                for block in data["content"]:
                    if block.get("type") == "text":
                        print(f"\n📝 文本内容: {block.get('text', '')}")
        else:
            print(f"\n❌ 请求失败!")
            print(f"响应内容: {resp.text}")


async def test_raw_endpoint():
    """测试原始端点"""
    print("\n" + "=" * 60)
    print("测试端点可用性")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GATEWAY_URL}/health")
        print(f"Health check: {resp.status_code} - {resp.json()}")

        resp = await client.get(f"{GATEWAY_URL}/")
        print(f"Root endpoint: {resp.status_code}")
        print(resp.json())


async def main():
    await test_raw_endpoint()
    await test_anthropic_endpoint()


if __name__ == "__main__":
    asyncio.run(main())
