"""End-to-end tests through a real MCP client connected in-process."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from odoo_mcp_server import server as server_module
from odoo_mcp_server.server import build_server, main


def run(coro_fn, *args):
    return anyio.run(coro_fn, *args)


async def _tool_names(svc):
    async with Client(build_server(svc)) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


def test_read_only_server_exposes_only_read_tools(service):
    tools = run(_tool_names, service)
    assert set(tools) == {
        "list_models",
        "describe_model",
        "search_records",
        "read_record",
        "count_records",
    }
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description


def test_write_tools_are_opt_in(make_service):
    tools = run(_tool_names, make_service(allow_write=True))
    assert {"create_record", "update_record"} <= set(tools)
    assert "delete_record" not in tools
    assert tools["update_record"].annotations.destructive_hint is True
    tools = run(_tool_names, make_service(allow_write=True, allow_delete=True))
    assert "delete_record" in tools


def test_tool_schema_documents_arguments(service):
    schema = run(_tool_names, service)["search_records"].input_schema
    assert schema["required"] == ["model"]
    assert "prefix notation" in json.dumps(schema["properties"]["domain"])


async def _call(svc, name, args):
    async with Client(build_server(svc)) as client:
        return await client.call_tool(name, args)


def test_search_records_returns_structured_json(service):
    result = run(
        _call,
        service,
        "search_records",
        {
            "model": "sale.order",
            "domain": [["state", "=", "sale"]],
            "fields": ["name", "partner_id", "amount_total"],
        },
    )
    assert result.is_error is False
    assert result.structured_content["records"] == [
        {
            "id": 10,
            "name": "S00010",
            "partner_id": {"id": 1, "name": "Azure Interior"},
            "amount_total": 1500.0,
        },
    ]
    assert json.loads(result.content[0].text)["total"] == 1


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"model": "ir.config_parameter"}, "always blocked"),
        ({"model": "res.partner", "domain": [["nmae", "=", "x"]]}, "Did you mean: name"),
        ({"model": "res.partner", "limit": 0}, "limit"),
    ],
)
def test_errors_are_returned_to_the_model(service, args, message):
    result = run(_call, service, "count_records" if "limit" not in args else "search_records", args)
    assert result.is_error is True
    assert message in result.content[0].text


def test_create_through_mcp(fake, make_service):
    svc = make_service(allow_write=True)
    result = run(
        _call, svc, "create_record", {"model": "res.partner", "values": {"name": "Via MCP"}}
    )
    assert result.structured_content["display_name"] == "Via MCP"


async def _prompts_and_resources(svc):
    async with Client(build_server(svc)) as client:
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        summary = await client.get_prompt("sales_summary", {"period": "2026-Q3"})
        info = await client.read_resource("odoo://server-info")
        schema = await client.read_resource("odoo://models/res.country")
        return prompts, summary, info, schema


def test_prompts_and_resources(service):
    prompts, summary, info, schema = run(_prompts_and_resources, service)
    assert prompts == {"sales_summary", "explore_model"}
    assert "2026-Q3" in summary.messages[0].content.text
    assert json.loads(info.contents[0].text)["mode"] == "read-only"
    fields = json.loads(schema.contents[0].text)["fields"]
    assert {f["name"] for f in fields} >= {"name", "code"}


# -- CLI ----------------------------------------------------------------------


def test_main_reports_config_errors(monkeypatch, capsys):
    for var in ("ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "ODOO_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert main([]) == 2
    assert "Missing required environment variable" in capsys.readouterr().err


def test_main_check(monkeypatch, capsys, service):
    monkeypatch.setenv("ODOO_URL", "http://odoo.test")
    monkeypatch.setenv("ODOO_DB", "testdb")
    monkeypatch.setenv("ODOO_USERNAME", "admin")
    monkeypatch.setenv("ODOO_API_KEY", "irrelevant")
    monkeypatch.setattr(server_module, "create_service", lambda settings: service)
    assert main(["--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["server_version"] == "19.0"
    assert "res.users.apikeys" in out["always_blocked_models"]
