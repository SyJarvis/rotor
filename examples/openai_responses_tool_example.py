import json
import os

from openai import OpenAI


base_url = os.getenv("ROTOR_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
client = OpenAI(api_key=os.environ["ROTOR_API_KEY"], base_url=f"{base_url}/v1")
model = os.getenv("ROTOR_MODEL", "glm-5.2")


def main() -> None:
    input_items = [{"role": "user", "content": "What is the weather in Shanghai?"}]
    tools = [{
        "type": "function",
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        "strict": True,
    }]

    response = client.responses.create(model=model, input=input_items, tools=tools)
    for item in response.output:
        if item.type != "function_call":
            continue
        arguments = json.loads(item.arguments)
        result = {"city": arguments["city"], "temperature_c": 30, "condition": "sunny"}
        input_items.extend([
            {
                "type": "function_call",
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            },
            {
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": json.dumps(result),
            },
        ])

    final = client.responses.create(model=model, input=input_items, tools=tools)
    print(final.output_text)


if __name__ == "__main__":
    main()
