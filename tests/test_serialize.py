from __future__ import annotations

import datetime as dt

import pytest

from odoo_mcp_server.errors import ValidationError
from odoo_mcp_server.serialize import (
    describe_field,
    prepare_values,
    serialize_record,
    serialize_value,
    to_jsonable,
)
from tests.conftest import FIELDS

PARTNER = FIELDS["res.partner"]


def test_record_serialization_normalises_odoo_quirks():
    raw = {
        "name": "Deco Addict",
        "id": 2,
        "email": False,
        "is_company": False,
        "country_id": [2, "Belgium"],
        "category_id": [1, 2],
        "image_1920": "12.5 Kb",
        "credit_limit": 500.0,
        "message_ids": False,
    }
    out = serialize_record(raw, PARTNER)
    assert next(iter(out)) == "id"
    assert out == {
        "id": 2,
        "name": "Deco Addict",
        "email": None,
        "is_company": False,
        "country_id": {"id": 2, "name": "Belgium"},
        "category_id": [1, 2],
        "image_1920": "<binary: 12.5 Kb>",
        "credit_limit": 500.0,
        "message_ids": [],
    }


@pytest.mark.parametrize(
    ("value", "meta", "expected"),
    [
        (False, {"type": "many2one"}, None),
        (7, {"type": "many2one"}, {"id": 7, "name": None}),
        (False, {"type": "date"}, None),
        (False, {"type": "selection"}, None),
        (0, {"type": "integer"}, 0),
        ("aGVsbG8gd29ybGQ=", {"type": "binary"}, "<binary: ~12 bytes>"),
        (False, {"type": "binary"}, None),
        ("x", None, "x"),
    ],
)
def test_serialize_value(value, meta, expected):
    assert serialize_value(value, meta) == expected


def test_to_jsonable_handles_rpc_types():
    payload = {
        "when": dt.datetime(2026, 1, 2, 3, 4, 5),
        "day": dt.date(2026, 1, 2),
        "blob": b"abc",
        "tuple": (1, 2),
        3: {1},
    }
    assert to_jsonable(payload) == {
        "when": "2026-01-02T03:04:05",
        "day": "2026-01-02",
        "blob": "<binary: 3 bytes>",
        "tuple": [1, 2],
        "3": [1],
    }


def test_describe_field_is_compact():
    info = describe_field("type", PARTNER["type"])
    assert info == {
        "name": "type",
        "label": "Address Type",
        "type": "selection",
        "selection": [
            {"value": "contact", "label": "Contact"},
            {"value": "invoice", "label": "Invoice Address"},
        ],
    }
    assert describe_field("display_name", PARTNER["display_name"])["stored"] is False


def test_prepare_values_accepts_friendly_shapes():
    values = {
        "name": "New",
        "country_id": {"id": 1, "name": "India"},
        "category_id": [3, 4],
        "email": None,
        "is_company": True,
        "credit_limit": 10,
        "type": "invoice",
    }
    assert prepare_values(values, PARTNER, "res.partner") == {
        "name": "New",
        "country_id": 1,
        "category_id": [(6, 0, [3, 4])],
        "email": False,
        "is_company": True,
        "credit_limit": 10,
        "type": "invoice",
    }


def test_prepare_values_passes_x2many_commands_through():
    out = prepare_values({"category_id": [[4, 9, 0]]}, PARTNER, "res.partner")
    assert out == {"category_id": [[4, 9, 0]]}
    out = prepare_values({"category_id": [{"id": 5, "name": "VIP"}]}, PARTNER, "res.partner")
    assert out == {"category_id": [(6, 0, [5])]}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({}, "non-empty"),
        ({"emial": "x"}, "Did you mean: email"),
        ({"id": 5}, "cannot be written"),
        ({"image_1920": "abc"}, "cannot be written"),
        ({"display_name": "x"}, "computed and read-only"),
        ({"country_id": "India"}, "pass a record id"),
        ({"category_id": "VIP"}, "list of ids"),
        ({"category_id": ["VIP"]}, "list of ids"),
        ({"is_company": "yes"}, "boolean"),
        ({"customer_rank": 1.5}, "integer"),
        ({"credit_limit": "lots"}, "number"),
        ({"type": "private"}, "Allowed: contact, invoice"),
    ],
)
def test_prepare_values_rejects_bad_input(values, message):
    with pytest.raises(ValidationError, match=message):
        prepare_values(values, PARTNER, "res.partner")
