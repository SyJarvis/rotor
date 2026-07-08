#!/usr/bin/env python3
"""Debug test to check response format"""
import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

async def test_glm_direct():
    """Test GLM API directly"""
    url = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('GLM_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "glm-4.7",
        "messages": [{"role": "user", "content": "你好，请用一句话介绍自己"}],
        "stream": False
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        print(f"GLM Status: {response.status_code}")
        print(f"GLM Response: {response.text[:1000]}")

async def test_kimi_direct():
    """Test Kimi API directly"""
    url = "https://api.kimi.com/coding/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('KIMI_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "kimi-k2.5",
        "messages": [{"role": "user", "content": "请用一句话解释什么是人工智能"}],
        "stream": False
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        print(f"Kimi Status: {response.status_code}")
        print(f"Kimi Response: {response.text[:1000]}")

async def test_minimax_direct():
    """Test MiniMax API directly"""
    url = "https://api.minimaxi.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('MINIMAX_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "MiniMax-M2.5",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
        "stream": False
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        print(f"MiniMax Status: {response.status_code}")
        print(f"MiniMax Response: {response.text[:1000]}")

async def test_gateway_endpoint():
    """Test the gateway endpoint"""
    # First, get a token
    url = "http://localhost:8000/api/admin/tokens/generate"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, params={"name": "debug-test"})
        if response.status_code not in (200, 201):
            print(f"Failed to get token: {response.status_code}")
            return

        token = response.json().get("key")
        print(f"Got token: {token[:20]}...")

        # Test GLM through gateway
        url = "http://localhost:8000/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "glm-4.7",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": False
        }

        response = await client.post(url, headers=headers, json=payload)
        print(f"\nGateway GLM Status: {response.status_code}")
        print(f"Gateway GLM Response: {response.text[:1000]}")

        # Try to parse as JSON
        try:
            data = response.json()
            print(f"Gateway GLM Parsed: {data}")
        except:
            pass

async def main():
    print("=" * 60)
    print("Testing GLM directly...")
    print("=" * 60)
    await test_glm_direct()

    print("\n" + "=" * 60)
    print("Testing Kimi directly...")
    print("=" * 60)
    await test_kimi_direct()

    print("\n" + "=" * 60)
    print("Testing MiniMax directly...")
    print("=" * 60)
    await test_minimax_direct()

    print("\n" + "=" * 60)
    print("Testing Gateway endpoint...")
    print("=" * 60)
    await test_gateway_endpoint()

if __name__ == "__main__":
    asyncio.run(main())
