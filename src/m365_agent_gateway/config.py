"""Small environment-backed local-service configuration."""

import math
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import GatewayError


@dataclass(frozen=True)
class Settings:
    model: str = "m365-agent"
    timeout: float = 120.0
    max_body: int = 16_000_000
    # Copilot drops websocket frames near 2 MB; 1.95M prompt chars measured OK.
    max_prompt: int = 1_950_000
    max_output: int = 4_000_000
    rate_per_minute: int = 60
    emulate_tools: bool = True
    port: int = 47821

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            timeout = float(os.environ.get("M365_TIMEOUT", "120"))
            port = int(os.environ.get("M365_PORT", "47821"))
            if not math.isfinite(timeout) or not 0 < timeout <= 600 or not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise GatewayError("M365_TIMEOUT or M365_PORT is invalid.") from None
        return cls(
            timeout=timeout,
            port=port,
            emulate_tools=os.environ.get("M365_EMULATE_TOOLS", "1") == "1",
        )


def state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "m365-agent-gateway"
