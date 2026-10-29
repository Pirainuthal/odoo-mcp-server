from __future__ import annotations

import pytest

from odoo_mcp_server.config import Settings
from odoo_mcp_server.errors import ConfigError

BASE = {
    "ODOO_URL": "https://erp.example.com/",
    "ODOO_DB": "prod",
    "ODOO_USERNAME": "bot@example.com",
    "ODOO_API_KEY": "secret-key",
}


def test_minimal_env_defaults_to_read_only():
    s = Settings.from_env(BASE)
    assert s.url == "https://erp.example.com"  # trailing slash stripped
    assert (s.allow_write, s.allow_delete) == (False, False)
    assert s.mode == "read-only"
    assert (s.default_limit, s.max_limit) == (50, 200)


def test_api_key_is_not_in_repr():
    assert "secret-key" not in repr(Settings.from_env(BASE))


def test_missing_variables_are_all_reported():
    with pytest.raises(ConfigError, match="ODOO_DB, ODOO_API_KEY"):
        Settings.from_env({"ODOO_URL": "http://x", "ODOO_USERNAME": "u"})


@pytest.mark.parametrize("url", ["erp.example.com", "ftp://erp", "https://"])
def test_invalid_url(url):
    with pytest.raises(ConfigError, match="ODOO_URL"):
        Settings.from_env({**BASE, "ODOO_URL": url})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("TRUE", True), ("yes", True), ("0", False), ("off", False), ("", False)],
)
def test_boolean_parsing(raw, expected):
    assert Settings.from_env({**BASE, "ODOO_MCP_ALLOW_WRITE": raw}).allow_write is expected


def test_invalid_boolean():
    with pytest.raises(ConfigError, match="ODOO_MCP_ALLOW_WRITE"):
        Settings.from_env({**BASE, "ODOO_MCP_ALLOW_WRITE": "maybe"})


def test_delete_requires_write():
    with pytest.raises(ConfigError, match="requires ODOO_MCP_ALLOW_WRITE"):
        Settings.from_env({**BASE, "ODOO_MCP_ALLOW_DELETE": "1"})
    s = Settings.from_env({**BASE, "ODOO_MCP_ALLOW_WRITE": "1", "ODOO_MCP_ALLOW_DELETE": "1"})
    assert s.mode == "read-write-delete"


def test_model_lists_and_limits():
    s = Settings.from_env(
        {
            **BASE,
            "ODOO_MCP_MODELS": " res.partner, sale.* ,",
            "ODOO_MCP_DENY_MODELS": "hr.*",
            "ODOO_MCP_DEFAULT_LIMIT": "10",
            "ODOO_MCP_MAX_LIMIT": "20",
            "ODOO_TIMEOUT": "5",
        }
    )
    assert s.allowed_models == ("res.partner", "sale.*")
    assert s.denied_models == ("hr.*",)
    assert (s.default_limit, s.max_limit, s.timeout) == (10, 20, 5.0)


def test_default_limit_cannot_exceed_max():
    with pytest.raises(ConfigError, match="cannot exceed"):
        Settings.from_env({**BASE, "ODOO_MCP_DEFAULT_LIMIT": "500"})


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_invalid_integers(raw):
    with pytest.raises(ConfigError, match="ODOO_MCP_MAX_LIMIT"):
        Settings.from_env({**BASE, "ODOO_MCP_MAX_LIMIT": raw})
