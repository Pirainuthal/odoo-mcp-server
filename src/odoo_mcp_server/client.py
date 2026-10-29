"""A small, thread-safe Odoo XML-RPC client authenticated with an API key.

Only two Odoo endpoints are used:

* ``/xmlrpc/2/common`` for ``version()`` and ``authenticate()``
* ``/xmlrpc/2/object`` for ``execute_kw()``

Every call runs as the API-key user, so Odoo's own access rights and record
rules always apply on top of this server's safety policy.

The proxies are injectable, which lets the test-suite swap in an in-memory
fake Odoo without any network access.
"""

from __future__ import annotations

import http.client
import threading
import xmlrpc.client
from typing import Any, Protocol

from .config import Settings
from .errors import OdooRPCError

# Fault codes sent by Odoo's /xmlrpc/2 endpoint (see odoo/addons/rpc).
_FAULT_KIND = {2: "user", 3: "auth", 4: "access"}
_EXCEPTION_KIND = {
    "AccessError": "access",
    "AccessDenied": "auth",
    "MissingError": "missing",
    "UserError": "user",
    "ValidationError": "user",
    "RedirectWarning": "user",
}


class CommonProxy(Protocol):
    def version(self) -> dict[str, Any]: ...

    def authenticate(self, db: str, login: str, key: str, env: dict[str, Any]) -> Any: ...


class ObjectProxy(Protocol):
    def execute_kw(
        self,
        db: str,
        uid: int,
        key: str,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any: ...


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _make_proxy(url: str, timeout: float) -> xmlrpc.client.ServerProxy:
    transport_cls = _TimeoutSafeTransport if url.startswith("https://") else _TimeoutTransport
    return xmlrpc.client.ServerProxy(
        url, transport=transport_cls(timeout), allow_none=True, use_builtin_types=True
    )


def translate_fault(fault: xmlrpc.client.Fault) -> OdooRPCError:
    """Convert an XML-RPC fault into a short, readable :class:`OdooRPCError`.

    Odoo sends the plain message for user/access errors and a full Python
    traceback for everything else; we keep only the final ``Type: message``
    line of a traceback so the model is not flooded with server internals.
    """
    code, text = fault.faultCode, str(fault.faultString or "").strip()
    if isinstance(code, int) and code in _FAULT_KIND:
        return OdooRPCError(text or "Odoo refused the request.", _FAULT_KIND[code])

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    last = lines[-1] if lines else str(code)
    exc_name, _, message = last.partition(": ")
    short_name = exc_name.rsplit(".", 1)[-1]
    if message and short_name.isidentifier():
        kind = _EXCEPTION_KIND.get(short_name, "server")
        return OdooRPCError(f"{short_name}: {message}", kind)
    return OdooRPCError(last, "server")


class OdooClient:
    """Thin wrapper over Odoo's external API.

    Authentication is lazy: the first call that needs a user id logs in and
    caches the uid. ``xmlrpc.client.ServerProxy`` is not thread-safe, so all
    calls are serialised with a lock (MCP runs sync tools in worker threads).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        common: CommonProxy | None = None,
        models: ObjectProxy | None = None,
    ) -> None:
        self.settings = settings
        self._common = common or _make_proxy(f"{settings.url}/xmlrpc/2/common", settings.timeout)
        self._models = models or _make_proxy(f"{settings.url}/xmlrpc/2/object", settings.timeout)
        self._uid: int | None = None
        self._version: dict[str, Any] | None = None
        self._lock = threading.RLock()

    # -- low level --------------------------------------------------------

    def _rpc(self, func: Any, *args: Any) -> Any:
        try:
            with self._lock:
                return func(*args)
        except xmlrpc.client.Fault as fault:
            raise translate_fault(fault) from None
        except xmlrpc.client.ProtocolError as err:
            raise OdooRPCError(
                f"HTTP {err.errcode} {err.errmsg} from {self.settings.url}. "
                "Check ODOO_URL (and that the 'rpc' module is installed on Odoo 19+).",
                "connection",
            ) from None
        except (OSError, http.client.HTTPException) as err:
            raise OdooRPCError(
                f"Cannot reach Odoo at {self.settings.url}: {err}", "connection"
            ) from None

    # -- public API -------------------------------------------------------

    def version(self) -> dict[str, Any]:
        """Return Odoo's ``version()`` payload (cached)."""
        if self._version is None:
            self._version = self._rpc(self._common.version)
        return self._version

    @property
    def uid(self) -> int:
        """The authenticated user id (logs in on first access)."""
        if self._uid is None:
            s = self.settings
            uid = self._rpc(self._common.authenticate, s.db, s.username, s.api_key, {})
            if not uid:
                raise OdooRPCError(
                    f"Authentication failed for {s.username!r} on database {s.db!r}. "
                    "Check ODOO_DB, ODOO_USERNAME and ODOO_API_KEY.",
                    "auth",
                )
            self._uid = int(uid)
        return self._uid

    def execute(
        self,
        model: str,
        method: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call ``model.method(*args, **kwargs)`` on the server.

        ``None`` keyword arguments are dropped so optional parameters fall
        back to Odoo's own defaults.
        """
        kw = {key: value for key, value in kwargs.items() if value is not None}
        s = self.settings
        return self._rpc(
            self._models.execute_kw, s.db, self.uid, s.api_key, model, method, list(args), kw
        )
