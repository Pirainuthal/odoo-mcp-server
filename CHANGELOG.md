# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-29

### Added
- MCP server built on the official MCP Python SDK (`MCPServer`, formerly `FastMCP`).
- Read tools: `list_models`, `describe_model`, `search_records`, `read_record`, `count_records`.
- Opt-in write tools: `create_record`, `update_record` (`ODOO_MCP_ALLOW_WRITE=1`) and
  `delete_record` (`ODOO_MCP_ALLOW_DELETE=1`).
- Safety policy: built-in model denylist, configurable allow/deny lists with glob patterns,
  hard result limit, domain / field / order validation, batch-size cap.
- Relation guard: dotted domain/order paths and `any` sub-domains are validated hop by hop
  and cannot reach a blocked model; nested x2many create/update commands obey the model
  policy; x2many commands that can delete records need `ODOO_MCP_ALLOW_DELETE=1`.
- Resources `odoo://server-info` and `odoo://models/{model}`; prompts `sales_summary`
  and `explore_model`.
- `odoo-mcp-server --check` connection test; stdio and streamable-HTTP transports.
- Unit tests with an in-memory fake Odoo, plus an opt-in live smoke test.
