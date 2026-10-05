"""OS-keyring-only persistence of explicitly captured browser sessions."""

import base64
import copy
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from keyring.errors import KeyringError, PasswordDeleteError

from .errors import GatewayError

SERVICE = "m365-agent-gateway"


class SecretStore:
    def __init__(self) -> None:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
        else:
            raise GatewayError("Only macOS and Linux are supported.", "platform", 503)
        self.backend = Keyring()

    def get(self, name: str) -> str | None:
        try:
            return self.backend.get_password(SERVICE, name)
        except (KeyringError, RuntimeError):
            raise GatewayError(
                "Unlock OS credential store and retry.", "credential_store", 503
            ) from None

    def put(self, name: str, value: str) -> None:
        try:
            self.backend.set_password(SERVICE, name, value)
        except (KeyringError, RuntimeError):
            raise GatewayError(
                "OS credential store cannot save secret.", "credential_store", 503
            ) from None

    def delete(self, name: str) -> None:
        try:
            self.backend.delete_password(SERVICE, name)
        except PasswordDeleteError:
            pass
        except (KeyringError, RuntimeError):
            raise GatewayError(
                "OS credential store cannot delete secret.", "credential_store", 503
            ) from None


class FileStore:
    """Read-only session file exported from the host, for containers without a keyring."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, name: str) -> str | None:
        if name != "session":
            return None
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError:
            raise GatewayError("Cannot read session file.", "credential_store", 503) from None

    def put(self, name: str, value: str) -> None:
        raise GatewayError(
            "Session file is read-only; log in on the host.", "credential_store", 503
        )

    def delete(self, name: str) -> None:
        self.put(name, "")


def export_session(store: SecretStore, directory: Path) -> Path:
    raw = Session.load(store)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / "session.json"
    temporary = directory / ".session.json.tmp"
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(json.dumps(asdict(raw)))
    # Same-directory rename keeps a directory bind mount in sync.
    os.replace(temporary, target)
    return target


def trusted_socket(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "wss"
            and parts.hostname == "substrate.office.com"
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and parts.path.startswith("/m365Copilot/Chathub/")
        )
    except ValueError:
        return False


def sanitize_template(template: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "source",
        "optionsSets",
        "allowedMessageTypes",
        "clientInfo",
        "options",
        "streamingMode",
        "spokenTextMode",
        "tone",
        "plugins",
        "renderReferencesBehindEOS",
    }
    result = copy.deepcopy({key: value for key, value in template.items() if key in allowed})
    result["message"] = {}
    return result


@dataclass(frozen=True)
class Session:
    socket_url: str = field(repr=False)
    template: dict[str, Any] = field(repr=False)
    token: str = field(repr=False)
    user: str
    tenant: str
    expires_at: int
    mailbox: str | None = field(default=None, repr=False)

    @classmethod
    def from_capture(
        cls, url: str, template: dict[str, Any], mailbox: str | None = None
    ) -> "Session":
        if not trusted_socket(url):
            raise GatewayError("Untrusted upstream destination.", "session_invalid", 503)
        try:
            token = parse_qs(urlsplit(url).query)["access_token"][0]
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(claims, dict):
                raise ValueError
            expiry = claims["exp"]
            if (
                not isinstance(expiry, int)
                or isinstance(expiry, bool)
                or expiry <= time.time() + 60
            ):
                raise ValueError
            if claims.get("aud") != "https://substrate.office.com/sydney":
                raise ValueError
            user, tenant = claims["oid"], claims["tid"]
            if not isinstance(user, str) or not isinstance(tenant, str) or not user or not tenant:
                raise ValueError
            if mailbox is not None and (
                not isinstance(mailbox, str)
                or not mailbox
                or len(mailbox) > 320
                or "\r" in mailbox
                or "\n" in mailbox
            ):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise GatewayError(
                "Session missing or expired; use login/refresh.", "login_required", 503
            ) from None
        return cls(url, sanitize_template(template), token, user, tenant, expiry, mailbox)

    def save(self, store: SecretStore) -> None:
        store.put("session", json.dumps(asdict(self)))

    @classmethod
    def load(cls, store: SecretStore) -> "Session":
        raw = store.get("session")
        if raw is None:
            raise GatewayError("Sign in using the login command.", "login_required", 503)
        try:
            value = json.loads(raw)
            return cls.from_capture(value["socket_url"], value["template"], value.get("mailbox"))
        except (ValueError, KeyError, TypeError):
            raise GatewayError(
                "Stored session invalid; sign in again.", "login_required", 503
            ) from None
