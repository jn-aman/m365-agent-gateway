import json
from pathlib import Path

from m365_agent_gateway.config import Settings


def test_vscode_example_matches_gateway_capabilities():
    root = Path(__file__).resolve().parents[1]
    raw = (root / "examples/vscode/chatLanguageModels.json").read_text()
    (provider,) = json.loads(raw)
    assert provider["vendor"] == "customendpoint"
    assert provider["apiType"] == "chat-completions"
    assert "apiKey" not in provider
    models = provider["models"]
    assert {model["id"] for model in models} == {
        "m365-agent",
        "m365-agent-quick",
        "m365-agent-think-deeper",
        "m365-agent-sonnet",
        "m365-agent-opus",
        "m365-agent-gpt-5.6",
        "m365-agent-gpt-5.6-think-deeper",
    }
    for model in models:
        assert model["url"] == f"http://127.0.0.1:{Settings().port}/v1/chat/completions"
        assert model["toolCalling"] is True
        assert model["streaming"] is True
        assert model["vision"] is True
        assert model["thinking"] is True
        assert model["maxInputTokens"] == 1_000_000
        assert model["maxOutputTokens"] == 1_000_000
    assert Settings().max_prompt == 1_950_000
    assert Settings().max_output >= 4 * 1_000_000
