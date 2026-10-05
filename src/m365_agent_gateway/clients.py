"""Private client configuration and explicit local launch support."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .errors import GatewayError


def write_configs(directory: Path, port: int) -> list[Path]:
    if not 1 <= port <= 65535:
        raise GatewayError("Invalid local port.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    base = f"http://127.0.0.1:{port}"
    environment = {
        "ANTHROPIC_BASE_URL": base,
        # Gateway is keyless; a placeholder only skips Claude Code's login prompt.
        "ANTHROPIC_AUTH_TOKEN": "local",
        "ANTHROPIC_MODEL": "m365-agent-sonnet",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "m365-agent-opus",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "m365-agent-sonnet",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m365-agent-quick",
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "m365-agent-gpt-5.6",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000000",
        "DISABLE_PROMPT_CACHING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    documents = {
        "claude.settings.json": json.dumps(
            {"env": environment, "permissions": {"defaultMode": "default"}}, indent=2
        ),
        "opencode.json": json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "m365/m365-agent",
                "permission": {"*": "ask"},
                "provider": {
                    "m365": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "M365 Agent",
                        "options": {"baseURL": base + "/v1"},
                        "models": {
                            "m365-agent": {
                                "name": "M365 Agent (experimental)",
                                "limit": {"context": 1000000, "output": 1000000},
                            }
                        },
                    }
                },
            },
            indent=2,
        ),
        "codex.toml": (
            'model = "m365-agent"\nmodel_provider = "m365"\n'
            'approval_policy = "untrusted"\nsandbox_mode = "workspace-write"\n'
            '[model_providers.m365]\nname = "M365 Agent"\n'
            f'base_url = "{base}/v1"\nwire_api = "responses"\n'
        ),
        "mcp.json": json.dumps(
            {
                "mcpServers": {
                    "m365-agent": {
                        "command": sys.executable,
                        "args": ["-m", "m365_agent_gateway", "mcp"],
                    }
                }
            },
            indent=2,
        ),
    }
    paths = [directory / name for name in documents]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise GatewayError("Client configs already exist; choose a new output directory.")
    for name, content in documents.items():
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content + "\n")
    return paths


def launch_client(name: str, directory: Path, port: int, extra: list[str]) -> int:
    executable = shutil.which(name)
    if executable is None:
        raise GatewayError(f"{name} is not installed.", "client_missing", 503)
    environment = os.environ.copy()
    environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
    if name == "claude":
        args = [
            executable,
            "--settings",
            str(directory / "claude.settings.json"),
            "--model",
            "m365-agent-sonnet",
            *extra,
        ]
    elif name == "opencode":
        environment["OPENCODE_CONFIG"] = str(directory / "opencode.json")
        args = [executable, *extra]
    else:
        provider = (
            '{name="M365 Agent",base_url="http://127.0.0.1:'
            + str(port)
            + '/v1",wire_api="responses"}'
        )
        args = [
            executable,
            "-c",
            'model="m365-agent"',
            "-c",
            'model_provider="m365"',
            "-c",
            "model_providers.m365=" + provider,
            *extra,
        ]
    return subprocess.run(args, env=environment, check=False).returncode
