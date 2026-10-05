import json
import subprocess
import sys

import httpx
from test_api import MODEL

from m365_agent_gateway.api import create_app
from m365_agent_gateway.config import Settings


async def test_mocked_coding_loop_handles_failing_test(tmp_path):
    program = tmp_path / "sample.py"
    program.write_text("value = 0\nassert value == 2\n")
    decisions = [
        {"name": "read_file", "arguments": {}},
        {"name": "edit_file", "arguments": {"value": 1}},
        {"name": "run_test", "arguments": {}},
        {"name": "edit_file", "arguments": {"value": 2}},
        {"name": "run_test", "arguments": {}},
    ]

    class ScriptedUpstream:
        def __init__(self):
            self.prompts = []

        async def stream(self, prompt, tone, images=()):
            self.prompts.append(prompt)
            index = len(self.prompts) - 1
            yield json.dumps(
                {
                    "text": "done" if index == len(decisions) else "",
                    "calls": [decisions[index]] if index < len(decisions) else [],
                }
            )

    upstream = ScriptedUpstream()
    app = create_app(Settings(), upstream)
    tools = [
        {"type": "function", "function": {"name": name, "parameters": schema}}
        for name, schema in [
            ("read_file", {"type": "object", "additionalProperties": False}),
            (
                "edit_file",
                {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            ),
            ("run_test", {"type": "object", "additionalProperties": False}),
        ]
    ]
    messages = [{"role": "user", "content": "Fix sample and verify test."}]
    test_codes = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        for _ in range(6):
            response = await client.post(
                "/v1/chat/completions",
                json={"model": MODEL, "messages": messages, "tools": tools},
            )
            assert response.status_code == 200
            answer = response.json()["choices"][0]["message"]
            messages.append(answer)
            for call in answer.get("tool_calls", []):
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"])
                if name == "read_file":
                    output = program.read_text()
                elif name == "edit_file":
                    program.write_text(f"value = {arguments['value']}\nassert value == 2\n")
                    output = "edited"
                else:
                    check = subprocess.run(
                        [sys.executable, str(program)],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    test_codes.append(check.returncode)
                    output = (
                        "test passed" if check.returncode == 0 else "AssertionError: test failed"
                    )
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    assert test_codes == [1, 0]
    assert "AssertionError" in upstream.prompts[3]
    assert answer["content"] == "done"
