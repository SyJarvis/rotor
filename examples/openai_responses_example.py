import os

from openai import OpenAI


base_url = os.getenv("ROTOR_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
api_key = os.environ["ROTOR_API_KEY"]
model = os.getenv("ROTOR_MODEL", "glm-5.2")

client = OpenAI(api_key=api_key, base_url=f"{base_url}/v1")


def main() -> None:
    response = client.responses.create(
        model=model,
        instructions="You are a helpful assistant.",
        input="Explain what an LLM API gateway does in one sentence.",
        max_output_tokens=1024,
    )
    print(response.output_text)


if __name__ == "__main__":
    main()
