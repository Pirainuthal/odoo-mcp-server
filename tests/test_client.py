from __future__ import annotations

import xmlrpc.client

import pytest

from odoo_mcp_server.client import OdooClient, translate_fault
from odoo_mcp_server.errors import OdooRPCError
from tests.conftest import make_settings


@pytest.mark.parametrize(("code", "kind"), [(2, "user"), (3, "auth"), (4, "access")])
def test_translate_fault_by_code(code, kind):
    err = translate_fault(xmlrpc.client.Fault(code, "Nope."))
    assert (str(err), err.kind) == ("Nope.", kind)


def test_translate_fault_keeps_only_last_traceback_line():
    tb = (
        'Traceback (most recent call last):\n  File "/odoo/models.py", line 1\n'
        "odoo.exceptions.MissingError: Record does not exist or has been deleted.\n"
    )
    err = translate_fault(xmlrpc.client.Fault(1, tb))
    assert str(err) == "MissingError: Record does not exist or has been deleted."
    assert err.kind == "missing"
    assert "File" not in str(err)


def test_translate_fault_without_exception_line():
    err = translate_fault(xmlrpc.client.Fault(1, "something odd"))
    assert (str(err), err.kind) == ("something odd", "server")


def test_uid_is_lazy_and_cached(fake):
    client = OdooClient(make_settings(), common=fake, models=fake)
    assert fake.auth_calls == 0
    assert client.uid == 2
    client.execute("res.partner", "search_count", [])
    client.execute("res.partner", "search_count", [])
    assert fake.auth_calls == 1


def test_bad_credentials(fake):
    client = OdooClient(make_settings(api_key="wrong"), common=fake, models=fake)
    with pytest.raises(OdooRPCError, match="Authentication failed") as info:
        client.execute("res.partner", "search_count", [])
    assert info.value.kind == "auth"


def test_none_kwargs_are_dropped(fake):
    client = OdooClient(make_settings(), common=fake, models=fake)
    client.execute("res.partner", "search_read", [], fields=["name"], limit=None, order=None)
    assert fake.calls[-1][3] == {"fields": ["name"]}


def test_version_is_cached(fake):
    client = OdooClient(make_settings(), common=fake, models=fake)
    assert client.version()["server_serie"] == "19.0"
    fake.version = None  # would explode if called again
    assert client.version()["server_serie"] == "19.0"


class _Broken:
    def __init__(self, exc):
        self.exc = exc

    def version(self):
        raise self.exc

    def authenticate(self, *args):
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (ConnectionRefusedError("refused"), "Cannot reach Odoo"),
        (TimeoutError("timed out"), "Cannot reach Odoo"),
        (xmlrpc.client.ProtocolError("x", 404, "Not Found", {}), "HTTP 404"),
    ],
)
def test_transport_errors_become_connection_errors(fake, exc, message):
    client = OdooClient(make_settings(), common=_Broken(exc), models=fake)
    with pytest.raises(OdooRPCError, match=message) as info:
        client.version()
    assert info.value.kind == "connection"


def test_real_proxies_are_built_for_http_and_https():
    for url in ("http://localhost:8069", "https://erp.example.com"):
        client = OdooClient(make_settings(url=url))
        assert client._models is not None
