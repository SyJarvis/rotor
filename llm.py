from openai import OpenAI

base_url = "http://127.0.0.1:8000/v1"
api_key = "sk-jRSJniTrOiCVzuoeotEQ1BVipEGVUSpXFQTkb5Hm5Co"

client = OpenAI(api_key=api_key, base_url=base_url)

def get_llm_response(client, model, prompt, max_tokens=1024):
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
        max_completion_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def main():
    model = ["glm-5.2", "glm-5.1", "kimi-k2.6", "deepseek-v4-pro", "deepseek-v4-flash", "mimo-v2.5-pro"]
    content = get_llm_response(client, model[0], "Hello, how are you?")
    print(content)

if __name__ == "__main__":
    main()