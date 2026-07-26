# Rotor Python Examples

Install the optional client SDKs:

```bash
pip install openai anthropic
```

Set the Rotor API key generated from the management page:

```bash
export ROTOR_API_KEY="sk-..."
export ROTOR_BASE_URL="http://127.0.0.1:8000"
export ROTOR_MODEL="glm-5.2"
```

Run an example:

```bash
python examples/openai_example.py
python examples/openai_responses_example.py
python examples/openai_responses_tool_example.py
python examples/anthropic_messages_example.py
```

Restart `rotor serve` after changing or reinstalling Rotor so the running
process uses the current gateway code.
