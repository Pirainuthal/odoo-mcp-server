"""Configuration loaded from environment variables.

All settings live in one immutable :class:`Settings` object so the rest of the
code never touches ``os.environ`` directly (which also keeps tests simple).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .errors import ConfigError

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _parse_bool(name: str, raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (1/0, true/false, yes/no), got {raw!r}.")


def _parse_int(name: str, raw: str | None, default: int, minimum: int = 1) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    return value


def _parse_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    """Runtime configuration. Build it with :meth:`from_env`."""

    url: str
    db: str
    username: str
    api_key: str = field(repr=False)
    allow_write: bool = False
    allow_delete: bool = False
    allowed_models: tuple[str, ...] = ()
    denied_models: tuple[str, ...] = ()
    default_limit: int = 50
    max_limit: int = 200
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.default_limit > self.max_limit:
            raise ConfigError(
                f"ODOO_MCP_DEFAULT_LIMIT ({self.default_limit}) cannot exceed "
                f"ODOO_MCP_MAX_LIMIT ({self.max_limit})."
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from ``env`` (defaults to ``os.environ``)."""
        env = os.environ if env is None else env

        missing = [
            name
            for name in ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_KEY")
            if not env.get(name, "").strip()
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". See the README 'Configuration' section."
            )

        url = env["ODOO_URL"].strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"ODOO_URL must look like https://host[:port], got {url!r}.")

        allow_write = _parse_bool("ODOO_MCP_ALLOW_WRITE", env.get("ODOO_MCP_ALLOW_WRITE"))
        allow_delete = _parse_bool("ODOO_MCP_ALLOW_DELETE", env.get("ODOO_MCP_ALLOW_DELETE"))
        if allow_delete and not allow_write:
            raise ConfigError("ODOO_MCP_ALLOW_DELETE=1 also requires ODOO_MCP_ALLOW_WRITE=1.")

        return cls(
            url=url,
            db=env["ODOO_DB"].strip(),
            username=env["ODOO_USERNAME"].strip(),
            api_key=env["ODOO_API_KEY"].strip(),
            allow_write=allow_write,
            allow_delete=allow_delete,
            allowed_models=_parse_list(env.get("ODOO_MCP_MODELS")),
            denied_models=_parse_list(env.get("ODOO_MCP_DENY_MODELS")),
            default_limit=_parse_int(
                "ODOO_MCP_DEFAULT_LIMIT", env.get("ODOO_MCP_DEFAULT_LIMIT"), 50
            ),
            max_limit=_parse_int("ODOO_MCP_MAX_LIMIT", env.get("ODOO_MCP_MAX_LIMIT"), 200),
            timeout=float(_parse_int("ODOO_TIMEOUT", env.get("ODOO_TIMEOUT"), 30)),
        )

    @property
    def mode(self) -> str:
        """Human-readable access mode."""
        if self.allow_delete:
            return "read-write-delete"
        if self.allow_write:
            return "read-write"
        return "read-only"
