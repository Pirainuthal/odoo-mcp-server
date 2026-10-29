from __future__ import annotations

import pytest

from odoo_mcp_server.errors import OdooRPCError, SafetyError, ValidationError


def _methods(fake):
    return [(model, method) for model, method, _, _ in fake.calls]


# -- discovery ----------------------------------------------------------------


def test_server_info_has_no_secrets(service):
    info = service.server_info()
    assert info["server_version"] == "19.0"
    assert info["mode"] == "read-only"
    assert info["uid"] == 2
    assert "test-api-key" not in repr(info)


def test_list_models_hides_blocked_and_transient(service):
    result = service.list_models()
    names = [m["model"] for m in result["models"]]
    assert names == ["res.partner", "res.country", "sale.order"]
    assert result["total"] == 3


def test_list_models_filter_and_transient(service):
    result = service.list_models(query="sale", include_transient=True)
    assert [m["model"] for m in result["models"]] == ["sale.order", "sale.advance.payment.inv"]
    assert result["models"][1]["transient"] is True


def test_list_models_respects_allowlist(make_service):
    svc = make_service(allowed_models=("sale.*",))
    assert [m["model"] for m in svc.list_models()["models"]] == ["sale.order"]


def test_describe_model(service):
    result = service.describe_model("sale.order")
    names = [f["name"] for f in result["fields"]]
    # required fields first
    assert names[:3] == ["date_order", "name", "partner_id"]
    partner = next(f for f in result["fields"] if f["name"] == "partner_id")
    assert partner["relation"] == "res.partner"
    # read-only server reports no write rights even if Odoo would allow them
    assert result["access"] == {"read": True, "create": False, "write": False, "unlink": False}


def test_describe_model_hides_technical_fields_and_filters(service):
    fields = [f["name"] for f in service.describe_model("res.partner")["fields"]]
    assert "message_ids" not in fields
    with_tech = service.describe_model("res.partner", include_technical=True)
    assert "message_ids" in [f["name"] for f in with_tech["fields"]]
    filtered = service.describe_model("res.partner", field_query="COUNTRY")
    assert [f["name"] for f in filtered["fields"]] == ["country_id"]


def test_describe_model_access_rights_fallback_for_older_odoo(fake, rw_service):
    fake.legacy_access_api = True
    access = rw_service.describe_model("sale.order")["access"]
    assert access == {"read": True, "create": True, "write": True, "unlink": False}


def test_describe_flags_blocked_relations(make_service):
    svc = make_service(denied_models=("res.country",))
    country = next(
        f for f in svc.describe_model("res.partner")["fields"] if f["name"] == "country_id"
    )
    assert country["relation_blocked"] is True


def test_fields_get_is_cached(fake, service):
    service.count_records("res.partner")
    service.count_records("res.partner")
    assert _methods(fake).count(("res.partner", "fields_get")) == 1


def test_unknown_model_message(service):
    with pytest.raises(ValidationError, match=r"does not exist.*list_models"):
        service.search_records("x.unknown")


def test_blocked_model_never_reaches_odoo(fake, service):
    with pytest.raises(SafetyError):
        service.search_records("ir.config_parameter")
    assert fake.calls == []


# -- reading ------------------------------------------------------------------


def test_search_records_default_projection(fake, service):
    result = service.search_records("res.partner", order="name")
    assert result["total"] == 2  # archived contact excluded by Odoo's active_test
    first = result["records"][0]
    assert {"id", "name", "display_name", "email", "phone", "active"} <= set(first)
    assert "image_1920" not in first
    assert "message_ids" not in first
    _, _, _args, kwargs = next(c for c in fake.calls if c[1] == "search_read")
    assert kwargs["limit"] == 50
    assert kwargs["context"] == {"bin_size": True}


def test_default_projection_includes_relations_as_id_name(service):
    order = service.search_records("sale.order", order="name")["records"][0]
    assert order["partner_id"] == {"id": 1, "name": "Azure Interior"}
    assert order["state"] == "sale"
    assert order["amount_total"] == 1500.0


def test_search_records_with_domain_fields_and_paging(service):
    result = service.search_records(
        "res.partner",
        domain=[["is_company", "=", True]],
        fields=["name", "email"],
        limit=1,
        order="name asc",
    )
    assert result["total"] == 2
    assert result["records"] == [{"id": 1, "name": "Azure Interior", "email": "azure@example.com"}]
    assert result["next_offset"] == 1
    page2 = service.search_records(
        "res.partner",
        domain='[["is_company", "=", true]]',
        fields=["name", "email"],
        limit=1,
        offset=1,
        order="name",
    )
    assert page2["records"] == [{"id": 2, "name": "Deco Addict", "email": None}]
    assert "next_offset" not in page2


def test_search_records_all_fields_skips_binary(service):
    record = service.search_records("res.partner", fields=["*"], limit=1)["records"][0]
    assert "image_1920" not in record
    assert "email" in record


def test_search_records_caps_limit(make_service):
    svc = make_service(default_limit=1, max_limit=2)
    result = svc.search_records("res.partner", limit=1000)
    assert result["limit"] == 2
    assert "capped" in result["note"]


def test_search_records_validates_before_calling_odoo(fake, service):
    with pytest.raises(ValidationError, match="Did you mean: name"):
        service.search_records("res.partner", domain=[["nmae", "=", "x"]])
    with pytest.raises(ValidationError):
        service.search_records("res.partner", order="name; delete")
    assert ("res.partner", "search_read") not in _methods(fake)


def test_read_record(service):
    record = service.read_record("res.partner", 3)["record"]  # archived records are readable
    assert record["name"] == "Old Contact"
    assert record["country_id"] is None
    assert "image_1920" not in record


def test_read_record_missing(service):
    with pytest.raises(OdooRPCError, match=r"No 'res\.partner' record with id 999") as info:
        service.read_record("res.partner", 999)
    assert info.value.kind == "missing"


@pytest.mark.parametrize("bad", [0, -1, True, "3"])
def test_read_record_bad_id(service, bad):
    with pytest.raises(ValidationError):
        service.read_record("res.partner", bad)


def test_count_records(service):
    assert service.count_records("sale.order", [["state", "=", "sale"]]) == {
        "model": "sale.order",
        "count": 1,
    }


def test_odoo_access_errors_are_surfaced(fake, service):
    fake.forbidden.add(("sale.order", "search_count"))
    with pytest.raises(OdooRPCError, match="not allowed") as info:
        service.count_records("sale.order")
    assert info.value.kind == "access"


# -- writing ------------------------------------------------------------------


def test_writes_refused_in_read_only_mode(fake, service):
    with pytest.raises(SafetyError, match="read-only"):
        service.create_record("res.partner", {"name": "X"})
    with pytest.raises(SafetyError, match="read-only"):
        service.update_record("res.partner", [1], {"name": "X"})
    with pytest.raises(SafetyError, match="ALLOW_DELETE"):
        service.delete_record("res.partner", [1])
    assert fake.calls == []


def test_delete_needs_its_own_flag(make_service):
    svc = make_service(allow_write=True)
    with pytest.raises(SafetyError, match="ALLOW_DELETE"):
        svc.delete_record("res.partner", [1])


def test_create_record(fake, rw_service):
    result = rw_service.create_record(
        "res.partner", {"name": "Gemini Furniture", "country_id": {"id": 1}, "category_id": [1]}
    )
    assert result == {
        "model": "res.partner",
        "id": 101,
        "display_name": "Gemini Furniture",
        "created": True,
    }
    assert fake.data["res.partner"][101]["country_id"] == 1
    assert fake.data["res.partner"][101]["category_id"] == [1]


def test_update_record(fake, rw_service):
    result = rw_service.update_record("res.partner", [1, 2], {"phone": "+91 99", "email": None})
    assert result == {"model": "res.partner", "updated_ids": [1, 2], "fields": ["email", "phone"]}
    assert fake.data["res.partner"][2]["phone"] == "+91 99"
    assert fake.data["res.partner"][1]["email"] is False


def test_update_with_missing_id_changes_nothing(fake, rw_service):
    with pytest.raises(OdooRPCError, match=r"\[999\].*Nothing was changed"):
        rw_service.update_record("res.partner", [1, 999], {"phone": "x"})
    assert ("res.partner", "write") not in _methods(fake)


def test_delete_record(fake, rw_service):
    result = rw_service.delete_record("sale.order", [11])
    assert result == {"model": "sale.order", "deleted": [{"id": 11, "display_name": "S00011"}]}
    assert 11 not in fake.data["sale.order"]


def test_write_to_blocked_model_refused(rw_service):
    with pytest.raises(SafetyError):
        rw_service.update_record("ir.config_parameter", [1], {"value": "x"})


# -- relational safety --------------------------------------------------------


def test_domain_path_is_checked_on_every_hop(fake, service):
    service.count_records("sale.order", [["partner_id.country_id.code", "=", "IN"]])
    with pytest.raises(ValidationError, match=r"Unknown field 'cod' on 'res\.country'.*code"):
        service.count_records("sale.order", [["partner_id.country_id.cod", "=", "IN"]])
    with pytest.raises(ValidationError, match="not a relational field"):
        service.count_records("res.partner", [["name.foo", "=", "x"]])
    assert ("res.country", "search_count") not in [(m, f) for m, f, _, _ in fake.calls]


def test_domain_cannot_traverse_into_blocked_model(fake, service):
    with pytest.raises(SafetyError, match=r"res\.users\.apikeys"):
        service.count_records("res.users", [["api_key_ids.name", "ilike", "x"]])
    with pytest.raises(SafetyError, match="blocked model"):
        service.count_records("res.users", [["api_key_ids", "any", [["name", "=", "x"]]]])
    with pytest.raises(SafetyError, match="blocked model"):
        service.count_records("res.users", [["api_key_ids", "ilike", "x"]])
    # Comparing raw ids does not query the blocked model, so it stays allowed.
    service.count_records("res.users", [["api_key_ids", "=", False]])
    assert not any(m == "res.users.apikeys" for m, _, _, _ in fake.calls)


def test_allowlist_also_applies_to_domain_paths(make_service):
    svc = make_service(allowed_models=("sale.order",))
    with pytest.raises(SafetyError, match="ODOO_MCP_MODELS"):
        svc.count_records("sale.order", [["partner_id.name", "ilike", "azure"]])
    with pytest.raises(ValidationError, match="does not sort by a related"):
        svc.search_records("sale.order", order="partner_id.name")
    svc.search_records("sale.order", order="partner_id.id desc, id")
    assert svc.count_records("sale.order", [["partner_id", "in", [1, 2]]])["count"] == 2


def test_any_subdomain_is_validated_against_comodel(service):
    service.count_records("sale.order", [["partner_id", "any", [["is_company", "=", True]]]])
    with pytest.raises(ValidationError, match=r"Unknown field 'is_compny' on 'res\.partner'"):
        service.count_records("sale.order", [["partner_id", "any", [["is_compny", "=", True]]]])
    with pytest.raises(ValidationError, match="needs a relational field"):
        service.count_records("sale.order", [["name", "any", [["id", "=", 1]]]])


def test_deprecated_not_equal_operator_is_normalised(fake, service):
    service.count_records("res.partner", [["name", "<>", "x"]])
    assert fake.calls[-1][2][0] == [["name", "!=", "x"]]


def test_update_cannot_delete_one2many_lines_without_delete_flag(fake, make_service):
    svc = make_service(allow_write=True)
    for value in ([], [5], [[5]], [[3, 1]], [[6, 0, []]]):
        with pytest.raises(SafetyError, match="ODOO_MCP_ALLOW_DELETE"):
            svc.update_record("sale.order", [10], {"order_line": value})
    with pytest.raises(SafetyError, match="Command 2"):
        svc.update_record("res.partner", [1], {"category_id": [[2, 1, 0]]})
    assert ("sale.order", "write") not in [(m, f) for m, f, _, _ in fake.calls]
    # Adding lines and linking are fine in write-only mode.
    svc.update_record("sale.order", [10], {"order_line": [[0, 0, {"name": "Extra"}]]})
    svc.update_record("res.partner", [1], {"category_id": [1, 2]})  # many2many replace


def test_one2many_set_is_allowed_on_create_and_with_delete_flag(rw_service, make_service):
    svc = make_service(allow_write=True)
    svc.create_record("res.partner", {"name": "New", "user_ids": []})
    rw_service.update_record("sale.order", [10], {"order_line": [[5]]})


def test_nested_commands_respect_model_policy(make_service):
    svc = make_service(allow_write=True)
    with pytest.raises(SafetyError, match=r"ir\.cron"):
        svc.update_record(
            "sale.order",
            [10],
            {"order_line": [[0, 0, {"name": "x", "cron_ids": [[0, 0, {"name": "job"}]]}]]},
        )
    allow = make_service(allow_write=True, allowed_models=("sale.order",))
    with pytest.raises(SafetyError, match=r"sale\.order\.line"):
        allow.update_record("sale.order", [10], {"order_line": [[0, 0, {"name": "x"}]]})
    with pytest.raises(ValidationError, match="Unknown x2many command 9"):
        svc.update_record("res.partner", [1], {"category_id": [[9, 1]]})
