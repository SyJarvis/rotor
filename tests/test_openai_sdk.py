#!/usr/bin/env python3
"""Test OpenAI SDK response parsing"""
import asyncio
import httpx
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

async def test_openai_sdk():
    """Test OpenAI SDK response parsing"""
    # First, get a token
    url = "http://localhost:8000/api/admin/tokens/generate"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, params={"name": "debug-test"})
        token = response.json().get("key")
        print(f"Got token: {token[:20]}...")

    # Test with OpenAI SDK
    openai_client = AsyncOpenAI(
        api_key=token,
        base_url="http://localhost:8000/v1",
    )

    print("\n" + "=" * 60)
    print("Testing OpenAI SDK with GLM...")
    print("=" * 60)

    response = await openai_client.chat.completions.create(
        model="glm-4.7",
        messages=[{"role": "user", "content": "你好"}],
    )

    print(f"Response object: {response}")
    print(f"Response type: {type(response)}")
    print(f"Response dict: {response.model_dump()}")
    print(f"Choices: {response.choices}")
    print(f"First choice: {response.choices[0]}")
    print(f"Message: {response.choices[0].message}")
    print(f"Content: {repr(response.choices[0].message.content)}")
    print(f"Content type: {type(response.choices[0].message.content)}")

if __name__ == "__main__":
    asyncio.run(test_openai_sdk())
