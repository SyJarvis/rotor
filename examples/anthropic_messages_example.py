import os

from anthropic import Anthropic


base_url = os.getenv("ROTOR_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
api_key = os.environ["ROTOR_API_KEY"]
model = os.getenv("ROTOR_MODEL", "glm-5")

# The Anthropic SDK appends /v1/messages, while Rotor exposes
# /anthropic/v1/messages.
client = Anthropic(
    api_key=api_key,
    base_url=f"{base_url}/anthropic",
    default_headers={"Authorization": f"Bearer {api_key}"},
)


def main() -> None:
    message = client.messages.create(
        model=model,
        system="You are a helpful assistant.",
        messages=[
            {
                "role": "user",
                "content": "Explain what an LLM API gateway does in one sentence.",
            }
        ],
        max_tokens=1024,
    )
    text = "".join(block.text for block in message.content if block.type == "text")
    print(text)


if __name__ == "__main__":
    main()
