# M365 Agent Gateway

Local gateway that exposes your Microsoft 365 Copilot session as OpenAI, Anthropic,
and MCP APIs, so VS Code, Claude Code, OpenCode, and Codex can use it.

Experimental and single-user. Not affiliated with Microsoft or Anthropic. Not a
license bypass. Copilot's internal web protocol can change without notice.

## Quick start

Needs Python 3.11+, `uv`, and an unlocked macOS Keychain or Linux Secret Service.

```bash
uv sync --extra dev
uv run playwright install chromium
uv run m365-agent-gateway login    # sign in, complete MFA, send one Copilot message
uv run m365-agent-gateway serve    # http://127.0.0.1:47821
```

Or run it in Docker with automatic session refresh:

```bash
uv run m365-agent-gateway docker-up
```

## Models

Each model ID maps to a Copilot tone. Availability depends on your account.

| Model ID | Label | Copilot tone |
| --- | --- | --- |
| `m365-agent` | Auto | `Magic` |
| `m365-agent-quick` | Quick response | `Gpt_5_5_Chat` |
| `m365-agent-think-deeper` | Think deeper | `Gpt_5_5_Reasoning` |
| `m365-agent-sonnet` | Claude Sonnet | `Claude_Sonnet` |
| `m365-agent-opus` | Claude Opus | `Claude_Opus` |
| `m365-agent-gpt-5.6` | GPT-5.6 Quick response | `Gpt_5_6_Chat` |
| `m365-agent-gpt-5.6-think-deeper` | GPT-5.6 Think deeper | `Gpt_5_6_Reasoning` |

- **Vision:** all models. Images are uploaded to Copilot and attached to the turn.
- **Thinking:** accepted on all models (`enabled` or `adaptive`). Copilot has no
  switch, so the tone decides; only Think deeper models reason. Reasoning
  summaries are filtered out.
- **Tools:** emulated through the prompt, validated against JSON Schema, and
  buffered until valid. Plain-text answers pass through. Clients execute tools.
- **Context:** clients may declare 1M input and output. Copilot drops request
  frames above about 2 MB, so prompts are capped at 1,950,000 characters (about
  450k-500k tokens) with a `context_limit` error. Output is capped at 4,000,000
  characters.

## APIs

| Endpoint | Format |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI Chat Completions, SSE, function calls |
| `POST /v1/responses` | OpenAI Responses, SSE, optional in-memory history |
| `POST /v1/messages` | Anthropic Messages, SSE, tool use |
| `POST /v1/messages/count_tokens` | Estimated token count |
| `GET /v1/models` | Model list |
| `GET /health` | Liveness |
| `m365-agent-gateway mcp` | MCP stdio: `copilot_chat`, `copilot_decide_tools`, `gateway_capabilities` |

No API key. The server listens on loopback only, rejects requests with a browser
`Origin`, and rate-limits locally. `serve` logs request lines, never bodies.

## VS Code (Copilot Chat)

1. **Chat: Manage Language Models** > **Add Models** > **Custom Endpoint** >
   **Chat Completions**.
2. In the opened `chatLanguageModels.json`, add the provider from
   [examples/vscode/chatLanguageModels.json](examples/vscode/chatLanguageModels.json).
   It declares all seven models with `toolCalling`, `vision`, and `thinking` on,
   and 1M input and output tokens.
3. Pick a model in the chat model picker.

On Remote SSH, containers, or Codespaces, forward the port. Organization BYOK
policies can block custom models.

## Claude Code (VS Code extension and CLI)

Add to VS Code user settings (**Preferences: Open User Settings (JSON)**), then
run **Developer: Reload Window**:

```json
"claudeCode.disableLoginPrompt": true,
"claudeCode.environmentVariables": [
	{ "name": "ANTHROPIC_BASE_URL", "value": "http://127.0.0.1:47821" },
	{ "name": "ANTHROPIC_AUTH_TOKEN", "value": "local" },
	{ "name": "ANTHROPIC_MODEL", "value": "m365-agent-sonnet" },
	{ "name": "ANTHROPIC_DEFAULT_OPUS_MODEL", "value": "m365-agent-opus" },
	{ "name": "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", "value": "M365 Claude Opus" },
	{ "name": "ANTHROPIC_DEFAULT_SONNET_MODEL", "value": "m365-agent-sonnet" },
	{ "name": "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "value": "M365 Claude Sonnet" },
	{ "name": "ANTHROPIC_DEFAULT_HAIKU_MODEL", "value": "m365-agent-quick" },
	{ "name": "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME", "value": "M365 Quick response" },
	{ "name": "ANTHROPIC_CUSTOM_MODEL_OPTION", "value": "m365-agent-gpt-5.6" },
	{ "name": "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME", "value": "M365 GPT-5.6 Quick response" },
	{ "name": "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "value": "1000000" },
	{ "name": "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "value": "1000000" },
	{ "name": "DISABLE_PROMPT_CACHING", "value": "1" },
	{ "name": "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "value": "1" }
]
```

- `disableLoginPrompt` skips Anthropic sign-in. `ANTHROPIC_AUTH_TOKEN` is a
  placeholder; the gateway ignores it.
- The extension checks credentials in `claudeCode.environmentVariables`, so set
  them there, not only in `~/.claude/settings.json`.
- `/model` shows Opus, Sonnet, Haiku, and GPT-5.6. Any other model:
  `/model m365-agent-think-deeper`. Gateway model discovery doesn't help: Claude
  Code only lists IDs containing `claude` or `anthropic`.
- Haiku is also Claude Code's background model; keep it on a fast tone.
- Prompts over the upstream limit return Anthropic's `prompt is too long` error,
  so Claude Code compacts and retries.
- CLI: put the same variables in the `env` block of `~/.claude/settings.json`, or
  run `uv run m365-agent-gateway client claude`.

Anthropic does not support Claude Code with non-Claude backends.

## Docker

The image only serves. Login needs a desktop browser and the OS keyring, so the
host signs in and exports the session to a private file that the container mounts
read-only.

```bash
uv run m365-agent-gateway docker-up              # start and keep the session fresh
uv run m365-agent-gateway docker-up --no-watch   # start and exit
```

`docker-up`:

1. Reuses a valid session, otherwise refreshes headlessly from the saved browser
   sign-in, and opens a login window only if that fails.
2. Exports `~/.local/state/m365-agent-gateway/docker/session.json` (mode 0600).
3. Runs `docker compose up -d --build`.
4. Refreshes and re-exports 10 minutes before each token expiry (tokens last about
   an hour). The container reads the new file without a restart. Ctrl+C stops
   refreshing; the container keeps running.

Manual equivalent:

```bash
uv run m365-agent-gateway login
uv run m365-agent-gateway export-session
docker compose up -d --build
# about hourly:
uv run m365-agent-gateway refresh --headless && uv run m365-agent-gateway export-session
```

Notes:

- Each headless refresh sends `hi` from a hidden browser, so a short chat appears
  in your Copilot history about once an hour.
- Port `127.0.0.1:47821` only. Never publish it on another interface: the API has
  no authentication. Stop any host `serve` first.
- Hardened: non-root user, read-only filesystem, no capabilities,
  `no-new-privileges`, `/health` healthcheck.
- Dependencies come from public PyPI. Behind TLS inspection, build with a CA
  bundle: `docker build --secret id=ca,src=/path/ca.pem .`.
- CI builds the image and smoke-tests it without a session.

### Prebuilt image

Tagged releases publish a multi-arch (amd64, arm64) image to GHCR with an SBOM
and signed build provenance:

```bash
docker pull ghcr.io/jn-aman/m365-agent-gateway:latest
gh attestation verify oci://ghcr.io/jn-aman/m365-agent-gateway:latest --owner jn-aman
```

To use it with Compose, replace `build: .` with
`image: ghcr.io/jn-aman/m365-agent-gateway:latest`.

To cut a release, run the Release workflow on `main` and pick `patch`, `minor`, or
`major`:

```bash
gh workflow run release.yml --ref main -f bump=patch
```

It runs all checks, bumps the version in `pyproject.toml` and `uv.lock`, commits,
tags `vX.Y.Z`, then publishes the image and the GitHub release from that tag.
Pushing a `vX.Y.Z` tag by hand also publishes it, provided it matches the version
in `pyproject.toml`.

## Other clients

```bash
uv run m365-agent-gateway client claude     # or: opencode, codex
uv run m365-agent-gateway configure --output DIR
```

`client` launches an installed client wired to the gateway without touching its
global config. `configure` writes standalone Claude, OpenCode, Codex, and MCP
configs and never overwrites files.

## CLI

| Command | Purpose |
| --- | --- |
| `login` | Interactive browser sign-in and session capture |
| `refresh [--headless]` | Renew session; `--headless` reuses the saved sign-in |
| `status` | Session expiry, no tokens |
| `logout` | Delete local session and browser state |
| `serve [--port] [--host]` | Run the HTTP API |
| `export-session [--output]` | Write session file for Docker |
| `docker-up [--output] [--no-watch]` | Docker end to end |
| `client NAME` / `configure` | Client launch and config |
| `mcp` | MCP stdio server |

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `M365_PORT` | `47821` | HTTP port |
| `M365_TIMEOUT` | `120` | Request timeout in seconds, max `600` |
| `M365_EMULATE_TOOLS` | `1` | `0` disables tool emulation |
| `M365_SESSION_FILE` | unset | Read session from a file instead of the keyring (set in Docker) |
| `M365_SESSION_DIR` | `~/.local/state/m365-agent-gateway/docker` | Host dir Compose mounts |
| `XDG_STATE_HOME` | `~/.local/state` | Parent of private state |

## Security and limits

- Session and browser state live only in the OS keyring (or the 0600 exported
  file for Docker). No plaintext fallback. TLS uses verified OS trust.
- Each request is a fresh Copilot conversation with the full transcript.
  Responses `store: true` history is in memory only (64 entries, 30 minutes).
- Only images in a request are uploaded. No telemetry.
- Schema-valid tool calls are not safe calls: keep client approvals and sandboxing,
  never allow unattended destructive tools. Grounded content can carry prompt
  injection.
- Unsupported: native function calling, system-role enforcement, raw reasoning
  blocks, audio, embeddings, remote MCP execution, custom Responses tool kinds,
  temperature. Token counts are character-based estimates.
- MFA, Conditional Access, and service terms stay authoritative. Microsoft
  controls cloud retention.

## Development

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest --cov=m365_agent_gateway
uv run pip-audit
```

Offline tests cover all API formats, tone routing, official SDK streams, MCP
stdio, the browser and socket lifecycle, and a scripted read, edit, test, repair
coding loop. They do not prove tenant access or model quality.
