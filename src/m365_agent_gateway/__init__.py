"""Local Microsoft 365 agent gateway."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("m365-agent-gateway")
except PackageNotFoundError:
    __version__ = "0+unknown"
