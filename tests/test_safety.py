from __future__ import annotations

import pytest

from odoo_mcp_server.errors import SafetyError, ValidationError
from odoo_mcp_server.safety import SafetyPolicy

KNOWN = {"id", "name", "email", "partner_id", "state", "date_order", "order_line"}


@pytest.fixture
def policy() -> SafetyPolicy:
    return SafetyPolicy()


# -- models -------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "res.users.apikeys",
        "res.users.apikeys.description",
        "ir.config_parameter",
        "ir.rule",
        "ir.model.access",
        "ir.actions.server",
        "ir.cron",
        "ir.cron.trigger",
        "ir.model.fields",
        "ir.module.module",
        "base.automation",
        "ir.asset",
    ],
)
def test_builtin_denylist_always_applies(model):
    permissive = SafetyPolicy(allow_write=True, allowed_models=("*",))
    with pytest.raises(SafetyError, match="always blocked"):
        permissive.check_model(model)


def test_custom_denylist_and_allowlist():
    policy = SafetyPolicy(allowed_models=("res.partner", "sale.*"), denied_models=("sale.report",))
    assert policy.check_model(" sale.order ") == "sale.order"
    assert policy.check_model("res.partner") == "res.partner"
    with pytest.raises(SafetyError, match="ODOO_MCP_DENY_MODELS"):
        policy.check_model("sale.report")
    with pytest.raises(SafetyError, match="allowlist"):
        policy.check_model("account.move")


@pytest.mark.parametrize("model", ["", "Res.Partner", "res partner", "res.partner;drop", None])
def test_invalid_model_names(policy, model):
    with pytest.raises(ValidationError):
        policy.check_model(model)


# -- mode ---------------------------------------------------------------------


def test_read_only_by_default(policy):
    with pytest.raises(SafetyError, match="read-only"):
        policy.check_write()
    with pytest.raises(SafetyError, match="ODOO_MCP_ALLOW_DELETE"):
        policy.check_delete()


def test_write_without_delete():
    policy = SafetyPolicy(allow_write=True)
    policy.check_write()
    with pytest.raises(SafetyError):
        policy.check_delete()


# -- limits and ids -----------------------------------------------------------


def test_limit_defaults_and_hard_cap():
    policy = SafetyPolicy(default_limit=25, max_limit=100)
    assert policy.clamp_limit(None) == 25
    assert policy.clamp_limit(10) == 10
    assert policy.clamp_limit(10_000) == 100
    with pytest.raises(ValidationError):
        policy.clamp_limit(0)
    with pytest.raises(ValidationError):
        policy.check_offset(-1)


def test_check_ids(policy):
    assert policy.check_ids(5) == [5]
    assert policy.check_ids([3, 3, 4]) == [3, 4]
    for bad in ([], [0], [True], ["1"], "1"):
        with pytest.raises(ValidationError):
            policy.check_ids(bad)
    with pytest.raises(SafetyError, match="at once"):
        policy.check_ids(list(range(1, 102)))


# -- fields and order ---------------------------------------------------------


def test_check_fields_suggests_close_names(policy):
    assert policy.check_fields(["name", "email", "name"], KNOWN, "res.partner") == ["name", "email"]
    with pytest.raises(ValidationError, match="Did you mean: email"):
        policy.check_fields(["emial"], KNOWN, "res.partner")


def test_order_clause(policy):
    assert policy.check_order("date_order  DESC, id", KNOWN, "sale.order") == "date_order DESC, id"
    assert policy.check_order("partner_id.name asc nulls last", KNOWN, "x") is not None
    assert policy.check_order("  ", KNOWN, "x") is None


@pytest.mark.parametrize(
    "order", ["name; DROP TABLE res_partner", "name desc limit 1", "(select 1)", "unknown_field"]
)
def test_order_clause_rejects_injection_and_unknown(policy, order):
    with pytest.raises(ValidationError):
        policy.check_order(order, KNOWN, "sale.order")


# -- domains ------------------------------------------------------------------


def test_valid_domains(policy):
    assert policy.parse_domain(None) == []
    assert policy.parse_domain([]) == []
    domain = ["|", ["name", "ilike", "acme"], ("email", "=", False), ["state", "in", "sale"]]
    assert policy.parse_domain(domain, KNOWN) == [
        "|",
        ["name", "ilike", "acme"],
        ["email", "=", False],
        ["state", "in", ["sale"]],
    ]


def test_domain_as_json_string(policy):
    assert policy.parse_domain('[["name", "=", "x"]]', KNOWN) == [["name", "=", "x"]]
    assert policy.parse_domain("", KNOWN) == []
    with pytest.raises(ValidationError, match="JSON list"):
        policy.parse_domain("name = x", KNOWN)


def test_domain_dotted_path_and_any(policy):
    domain = [
        ["partner_id.country_id.code", "=", "IN"],
        ["order_line", "any", [["product_id.name", "ilike", "desk"]]],
    ]
    assert policy.parse_domain(domain, KNOWN) == domain


def test_true_false_leaves(policy):
    assert policy.parse_domain([True], KNOWN) == [[1, "=", 1]]
    assert policy.parse_domain([[1, "=", 1]], KNOWN) == [[1, "=", 1]]


@pytest.mark.parametrize(
    ("domain", "message"),
    [
        ({"name": "x"}, "must be a list"),
        ([["name", "="]], r"\[field, operator, value\]"),
        ([["name", "regex", "x"]], "Unsupported operator"),
        ([["nmae", "=", "x"]], "Did you mean: name"),
        ([["name);--", "=", "x"]], "Invalid field path"),
        ([["name", "=", {"$ne": 1}]], "Unsupported value"),
        ([["order_line", "any", "x"]], "sub-domain"),
        (["&", ["name", "=", "x"]], "needs 2"),
        (["!"], "needs 1"),
        (["AND", ["name", "=", "x"]], "Unexpected domain token"),
        ([42], "Invalid domain element"),
    ],
)
def test_invalid_domains(policy, domain, message):
    with pytest.raises(ValidationError, match=message):
        policy.parse_domain(domain, KNOWN, "sale.order")
