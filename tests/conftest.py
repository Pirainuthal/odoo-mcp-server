"""Shared fixtures: an in-memory fake Odoo speaking the XML-RPC proxy protocol."""

from __future__ import annotations

import copy
import xmlrpc.client
from typing import Any

import pytest

from odoo_mcp_server.client import OdooClient
from odoo_mcp_server.config import Settings
from odoo_mcp_server.safety import SafetyPolicy
from odoo_mcp_server.service import OdooService

API_KEY = "test-api-key-not-real"

FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "res.partner": {
        "id": {"string": "ID", "type": "integer", "readonly": True},
        "name": {"string": "Name", "type": "char", "required": True},
        "display_name": {
            "string": "Display Name",
            "type": "char",
            "readonly": True,
            "store": False,
        },
        "email": {"string": "Email", "type": "char"},
        "phone": {"string": "Phone", "type": "char"},
        "is_company": {"string": "Is a Company", "type": "boolean"},
        "active": {"string": "Active", "type": "boolean"},
        "customer_rank": {"string": "Customer Rank", "type": "integer"},
        "credit_limit": {"string": "Credit Limit", "type": "float"},
        "type": {
            "string": "Address Type",
            "type": "selection",
            "selection": [["contact", "Contact"], ["invoice", "Invoice Address"]],
        },
        "country_id": {"string": "Country", "type": "many2one", "relation": "res.country"},
        "category_id": {"string": "Tags", "type": "many2many", "relation": "res.partner.category"},
        "user_ids": {"string": "Users", "type": "one2many", "relation": "res.users"},
        "image_1920": {"string": "Image", "type": "binary"},
        "message_ids": {"string": "Messages", "type": "one2many", "relation": "mail.message"},
    },
    "res.country": {
        "id": {"string": "ID", "type": "integer", "readonly": True},
        "name": {"string": "Country Name", "type": "char", "required": True},
        "code": {"string": "Country Code", "type": "char"},
        "display_name": {
            "string": "Display Name",
            "type": "char",
            "readonly": True,
            "store": False,
        },
    },
    "sale.order": {
        "id": {"string": "ID", "type": "integer", "readonly": True},
        "name": {"string": "Order Reference", "type": "char", "required": True},
        "display_name": {
            "string": "Display Name",
            "type": "char",
            "readonly": True,
            "store": False,
        },
        "partner_id": {
            "string": "Customer",
            "type": "many2one",
            "relation": "res.partner",
            "required": True,
        },
        "state": {
            "string": "Status",
            "type": "selection",
            "selection": [["draft", "Quotation"], ["sale", "Sales Order"], ["cancel", "Cancelled"]],
        },
        "amount_total": {"string": "Total", "type": "monetary", "readonly": True},
        "date_order": {"string": "Order Date", "type": "datetime", "required": True},
        "order_line": {"string": "Order Lines", "type": "one2many", "relation": "sale.order.line"},
    },
    "sale.order.line": {
        "id": {"string": "ID", "type": "integer", "readonly": True},
        "name": {"string": "Description", "type": "text", "required": True},
        "order_id": {"string": "Order", "type": "many2one", "relation": "sale.order"},
        "product_uom_qty": {"string": "Quantity", "type": "float"},
        "tax_ids": {"string": "Taxes", "type": "many2many", "relation": "account.tax"},
        "cron_ids": {"string": "Crons (test only)", "type": "one2many", "relation": "ir.cron"},
    },
    "res.partner.category": {
        "id": {"string": "ID", "type": "integer", "readonly": True},
        "name": {"string": "Tag Name", "type": "char", "required": True},
    },
    "res.users": {
        "id": {"string": "ID", "type": "integer", "readonly": True},
        "login": {"string": "Login", "type": "char", "required": True},
        "api_key_ids": {"string": "API Keys", "type": "one2many", "relation": "res.users.apikeys"},
    },
}

M2O_TARGET = {
    ("res.partner", "country_id"): "res.country",
    ("sale.order", "partner_id"): "res.partner",
}


def _seed() -> dict[str, dict[int, dict[str, Any]]]:
    return {
        "res.country": {
            1: {"id": 1, "name": "India", "code": "IN"},
            2: {"id": 2, "name": "Belgium", "code": "BE"},
        },
        "res.partner": {
            1: {
                "id": 1,
                "name": "Azure Interior",
                "email": "azure@example.com",
                "phone": False,
                "is_company": True,
                "active": True,
                "customer_rank": 3,
                "credit_limit": 0.0,
                "type": "contact",
                "country_id": 1,
                "category_id": [],
                "image_1920": False,
                "message_ids": [],
            },
            2: {
                "id": 2,
                "name": "Deco Addict",
                "email": False,
                "phone": "+32 1",
                "is_company": True,
                "active": True,
                "customer_rank": 1,
                "credit_limit": 500.0,
                "type": "contact",
                "country_id": 2,
                "category_id": [1, 2],
                "image_1920": "12.5 Kb",
                "message_ids": [7],
            },
            3: {
                "id": 3,
                "name": "Old Contact",
                "email": False,
                "phone": False,
                "is_company": False,
                "active": False,
                "customer_rank": 0,
                "credit_limit": 0.0,
                "type": "invoice",
                "country_id": False,
                "category_id": [],
                "image_1920": False,
                "message_ids": [],
            },
        },
        "sale.order.line": {},
        "res.partner.category": {1: {"id": 1, "name": "VIP"}, 2: {"id": 2, "name": "B2B"}},
        "res.users": {},
        "sale.order": {
            10: {
                "id": 10,
                "name": "S00010",
                "partner_id": 1,
                "state": "sale",
                "amount_total": 1500.0,
                "date_order": "2026-10-01 09:00:00",
            },
            11: {
                "id": 11,
                "name": "S00011",
                "partner_id": 2,
                "state": "draft",
                "amount_total": 99.5,
                "date_order": "2026-10-05 10:30:00",
            },
        },
    }


class FakeOdoo:
    """Implements just enough of Odoo's ``common`` and ``object`` services."""

    def __init__(self) -> None:
        self.data = _seed()
        self.calls: list[tuple[str, str, list[Any], dict[str, Any]]] = []
        self.auth_calls = 0
        self.valid_key = API_KEY
        self.forbidden: set[tuple[str, str]] = set()  # (model, method) -> AccessError
        self.legacy_access_api = False  # simulate Odoo 17 (no has_access)
        self.next_id = 100

    # -- common -----------------------------------------------------------
    def version(self) -> dict[str, Any]:
        return {"server_version": "19.0", "server_serie": "19.0", "protocol_version": 1}

    def authenticate(self, db: str, login: str, key: str, env: dict[str, Any]) -> Any:
        self.auth_calls += 1
        return 2 if (db == "testdb" and login == "admin" and key == self.valid_key) else False

    # -- object -----------------------------------------------------------
    def execute_kw(self, db, uid, key, model, method, args, kwargs):
        assert key == self.valid_key
        assert uid == 2
        self.calls.append((model, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
        if (model, method) in self.forbidden:
            raise xmlrpc.client.Fault(4, f"You are not allowed to {method} '{model}' records.")
        if model == "ir.model":
            return self._ir_model(method, args, kwargs)
        if model not in FIELDS:
            raise xmlrpc.client.Fault(
                1, f"Traceback (most recent call last):\n  File ...\nKeyError: '{model}'\n"
            )
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            raise xmlrpc.client.Fault(
                1,
                f"Traceback...\nAttributeError: The method '{method}' does not exist "
                f"on the model '{model}'",
            )
        return handler(model, *args, **kwargs)

    def _ir_model(self, method, args, kwargs):
        rows = [
            {"id": 1, "model": "res.partner", "name": "Contact", "transient": False},
            {"id": 2, "model": "res.country", "name": "Country", "transient": False},
            {"id": 3, "model": "sale.order", "name": "Sales Order", "transient": False},
            {
                "id": 4,
                "model": "ir.config_parameter",
                "name": "System Parameter",
                "transient": False,
            },
            {"id": 5, "model": "res.users.apikeys", "name": "Users API Keys", "transient": False},
            {
                "id": 6,
                "model": "sale.advance.payment.inv",
                "name": "Down Payment Wizard",
                "transient": True,
            },
        ]
        domain = args[0] if args else []
        return [dict(r) for r in rows if _match(r, domain)]

    # model methods ----------------------------------------------------------
    def _m_fields_get(self, model, allfields=None, attributes=None, **_):
        return copy.deepcopy(FIELDS[model])

    def _rows(self, model, domain, context=None):
        active_test = (context or {}).get("active_test", True)
        has_active = "active" in FIELDS[model]
        uses_active = any(isinstance(t, list) and t[0] == "active" for t in domain)
        rows = []
        for rec in self.data[model].values():
            if has_active and active_test and not uses_active and not rec.get("active", True):
                continue
            if _match(rec, domain):
                rows.append(rec)
        return rows

    def _render(self, model, rec, fields):
        out = {"id": rec["id"]}
        for name in fields:
            if name == "id":
                continue
            if name == "display_name":
                out[name] = rec["name"]
                continue
            value = rec.get(name, False)
            target = M2O_TARGET.get((model, name))
            if target and value:
                value = [value, self.data[target][value]["name"]]
            out[name] = value
        return out

    def _m_search_read(
        self, model, domain=(), fields=None, offset=0, limit=None, order=None, context=None
    ):
        rows = self._rows(model, list(domain), context)
        if order:
            key, _, direction = order.split(",")[0].strip().partition(" ")
            rows.sort(key=lambda r: (r.get(key) is False, r.get(key)), reverse="desc" in direction)
        rows = rows[offset : offset + limit if limit else None]
        fields = fields or list(FIELDS[model])
        return [self._render(model, r, fields) for r in rows]

    def _m_search_count(self, model, domain=(), context=None, **_):
        return len(self._rows(model, list(domain), context))

    def _m_create(self, model, values, context=None):
        self.next_id += 1
        rec = {"id": self.next_id, "active": True}
        rec.update(_apply_commands(values))
        self.data[model][self.next_id] = rec
        return self.next_id

    def _m_write(self, model, ids, values, context=None):
        for i in ids:
            self.data[model][i].update(_apply_commands(values))
        return True

    def _m_unlink(self, model, ids, context=None):
        for i in ids:
            del self.data[model][i]
        return True

    def _m_has_access(self, model, ids, operation):
        if self.legacy_access_api:
            raise xmlrpc.client.Fault(1, "Traceback...\nAttributeError: has_access")
        return (model, operation) not in {("sale.order", "unlink")}

    def _m_check_access_rights(self, model, operation, raise_exception=True):
        return operation != "unlink"


def _apply_commands(values: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], (list, tuple))
            and value[0][0] == 6
        ):
            value = list(value[0][2])
        out[key] = value
    return out


def _leaf(rec: dict[str, Any], leaf: list[Any]) -> bool:
    field, op, value = leaf
    if field in (0, 1):
        return field == value
    current = rec.get(field.split(".")[0], False)
    if op == "=":
        return current == value
    if op in ("!=", "<>"):
        return current != value
    if op == ">":
        return current is not False and current > value
    if op == "<":
        return current is not False and current < value
    if op == "ilike":
        return bool(current) and str(value).lower() in str(current).lower()
    if op == "in":
        return current in value
    if op == "not in":
        return current not in value
    if op in ("any", "not any"):  # sub-domains are not evaluated by the fake
        return (op == "any") == bool(current)
    raise NotImplementedError(op)


def _match(rec: dict[str, Any], domain: list[Any]) -> bool:
    """Evaluate a prefix-notation domain (implicit AND between top-level terms)."""

    def parse(pos: int) -> tuple[bool, int]:
        token = domain[pos]
        if token == "!":
            val, nxt = parse(pos + 1)
            return not val, nxt
        if token in ("&", "|"):
            left, nxt = parse(pos + 1)
            right, nxt = parse(nxt)
            return (left and right) if token == "&" else (left or right), nxt
        return _leaf(rec, token), pos + 1

    pos, result = 0, True
    while pos < len(domain):
        val, pos = parse(pos)
        result = result and val
    return result


# -- fixtures ---------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "url": "http://odoo.test",
        "db": "testdb",
        "username": "admin",
        "api_key": API_KEY,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def fake() -> FakeOdoo:
    return FakeOdoo()


@pytest.fixture
def make_service(fake: FakeOdoo):
    def factory(**overrides: Any) -> OdooService:
        settings = make_settings(**overrides)
        client = OdooClient(settings, common=fake, models=fake)
        return OdooService(client, SafetyPolicy.from_settings(settings))

    return factory


@pytest.fixture
def service(make_service) -> OdooService:
    return make_service()


@pytest.fixture
def rw_service(make_service) -> OdooService:
    return make_service(allow_write=True, allow_delete=True)
