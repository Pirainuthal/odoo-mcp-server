"""Exception hierarchy.

Every error raised on purpose by this package derives from :class:`OdooMCPError`.
The MCP layer turns these into tool errors whose message is shown to the model,
so messages are written to be actionable: they say what was wrong and what to
do instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from difflib import get_close_matches


class OdooMCPError(Exception):
    """Base class for all anticipated errors."""


class ConfigError(OdooMCPError):
    """The server is mis-configured (missing or invalid environment variables)."""


class SafetyError(OdooMCPError):
    """A request was refused by the server's safety policy."""


class ValidationError(OdooMCPError):
    """A tool argument (domain, fields, order, values...) is malformed."""


class OdooRPCError(OdooMCPError):
    """Odoo rejected a call (access error, missing record, user error...).

    ``kind`` is a short machine-friendly label such as ``"access"``,
    ``"missing"``, ``"user"``, ``"auth"``, ``"connection"`` or ``"server"``.
    """

    def __init__(self, message: str, kind: str = "server") -> None:
        super().__init__(message)
        self.kind = kind


def did_you_mean(name: str, candidates: Iterable[str]) -> str:
    """Return a ``" Did you mean: a, b?"`` hint, or an empty string."""
    matches = get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f" Did you mean: {', '.join(matches)}?" if matches else ""
