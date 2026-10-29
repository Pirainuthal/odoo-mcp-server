"""Optional smoke test against a real Odoo server.

Skipped unless ODOO_MCP_LIVE_TEST=1 and the ODOO_* variables are set. Only
read operations are performed unless ODOO_MCP_LIVE_WRITE=1, in which case a
temporary partner is created, updated and deleted again.
"""

from __future__ import annotations

import os
import uuid

import pytest

from odoo_mcp_server.config import Settings
from odoo_mcp_server.server import create_service

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("ODOO_MCP_LIVE_TEST") != "1", reason="ODOO_MCP_LIVE_TEST!=1"),
]


@pytest.fixture(scope="module")
def live():
    return create_service(Settings.from_env())


def test_live_read_flow(live):
    info = live.server_info()
    assert info["uid"] > 0
    models = live.list_models(query="res.partner")["models"]
    assert any(m["model"] == "res.partner" for m in models)
    assert not any(
        m["model"] == "res.users.apikeys" for m in live.list_models(limit=1000)["models"]
    )
    schema = live.describe_model("res.partner")
    assert schema["access"]["read"] is True
    page = live.search_records("res.partner", fields=["name", "country_id"], limit=5, order="id")
    assert page["returned"] <= 5
    assert live.count_records("res.partner")["count"] == page["total"]
    first = page["records"][0]
    assert live.read_record("res.partner", first["id"], ["name"])["record"]["name"] == first["name"]


@pytest.mark.skipif(os.environ.get("ODOO_MCP_LIVE_WRITE") != "1", reason="ODOO_MCP_LIVE_WRITE!=1")
def test_live_write_flow(live):
    if not live.policy.allow_delete:
        pytest.skip("needs ODOO_MCP_ALLOW_WRITE=1 and ODOO_MCP_ALLOW_DELETE=1")
    name = f"odoo-mcp-server smoke {uuid.uuid4().hex[:8]}"
    created = live.create_record("res.partner", {"name": name, "email": None})
    try:
        live.update_record("res.partner", [created["id"]], {"phone": "+91 00000 00000"})
        record = live.read_record("res.partner", created["id"], ["phone"])["record"]
        assert record["phone"] == "+91 00000 00000"
    finally:
        live.delete_record("res.partner", [created["id"]])
    assert live.count_records("res.partner", [["id", "=", created["id"]]])["count"] == 0
