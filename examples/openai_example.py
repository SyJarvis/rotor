import os

from openai import OpenAI

base_url = os.getenv("ROTOR_BASE_URL", "http://192.168.0.101:8000").rstrip("/")
api_key = os.environ["ROTOR_API_KEY"]
model = os.getenv("ROTOR_MODEL", "glm-5.2")

client = OpenAI(api_key=api_key, base_url=f"{base_url}/v1")


def get_llm_response(client: OpenAI, model: str, prompt: str, max_tokens: int = 1024) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def main():
    content = get_llm_response(client, model, "Hello, how are you?")
    print(content)


if __name__ == "__main__":
    main()
