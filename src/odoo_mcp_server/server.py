"""MCP server: exposes :class:`~odoo_mcp_server.service.OdooService` as tools,
resources and prompts using the official MCP Python SDK (``MCPServer``,
formerly ``FastMCP``).

Note: no ``from __future__ import annotations`` here - the SDK evaluates tool
signatures at registration time and some annotations reference local values.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .client import OdooClient
from .config import Settings
from .errors import ConfigError, OdooMCPError
from .safety import BUILTIN_DENIED_MODELS, SafetyPolicy
from .service import OdooService

log = logging.getLogger("odoo_mcp_server")
T = TypeVar("T")

# -- reusable argument types (descriptions become the tools' JSON schema) --

Model = Annotated[
    str,
    Field(description="Technical model name, e.g. 'res.partner', 'sale.order', 'account.move'."),
]
Domain = Annotated[
    list[Any] | None,
    Field(
        description=(
            "Odoo domain: a list of [field, operator, value] conditions, optionally combined "
            "with '&', '|', '!' in prefix notation. Conditions are AND-ed by default. "
            'Example: [["customer_rank", ">", 0], ["country_id.code", "=", "IN"]]. '
            "Omit or pass [] to match all records."
        )
    ),
]
Fields = Annotated[
    list[str] | None,
    Field(
        description=(
            "Field names to return. Omit for a compact default set; pass ['*'] for all "
            "non-binary fields. Use describe_model to discover field names."
        )
    ),
]
RecordIds = Annotated[
    list[int],
    Field(description="Ids of the records to act on (max 100 per call).", min_length=1),
]
Values = Annotated[
    dict[str, Any],
    Field(
        description=(
            "Field values. many2one: a record id (or {'id': ..}); many2many/one2many: a list "
            "of ids (replaces the set) or Odoo command lists; null clears a field."
        )
    ),
]

INSTRUCTIONS = """\
Tools for working with an Odoo ERP database through its external API.

Workflow tips:
- Use list_models to find technical model names, then describe_model to learn
  the fields of a model before searching or writing.
- Prefer count_records for "how many" questions and search_records with a
  small `fields` list for everything else; results are paginated (see
  `next_offset`).
- many2one values are returned as {"id", "name"}; empty values are null.
- All calls run as the configured Odoo user, so Odoo access rights apply.
- The server may be read-only; write tools are only listed when enabled.
"""


def _guard(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a service call, turning anticipated errors into MCP tool errors."""
    try:
        return fn(*args, **kwargs)
    except OdooMCPError as err:
        raise ToolError(str(err)) from err


def build_server(service: OdooService) -> MCPServer:
    """Create the MCP server and register tools, resources and prompts."""
    settings = service.client.settings
    policy = service.policy
    mcp = MCPServer(
        name="odoo",
        title="Odoo",
        description="Safe access to an Odoo ERP database for AI assistants.",
        instructions=INSTRUCTIONS,
        website_url="https://github.com/Pirainuthal/odoo-mcp-server",
        version=__version__,
    )
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    # -- read tools ---------------------------------------------------------

    @mcp.tool(title="List Odoo models", annotations=read_only)
    def list_models(
        query: Annotated[
            str | None,
            Field(description="Case-insensitive filter on technical name or label, e.g. 'sale'."),
        ] = None,
        include_transient: Annotated[
            bool, Field(description="Include transient (wizard) models.")
        ] = False,
        limit: Annotated[int | None, Field(description="Maximum models to return.", ge=1)] = None,
    ) -> dict[str, Any]:
        """List the Odoo models this server can access, optionally filtered by name.

        Models blocked by the safety policy are never listed.
        """
        return _guard(service.list_models, query, include_transient, limit)

    @mcp.tool(title="Describe an Odoo model", annotations=read_only)
    def describe_model(
        model: Model,
        field_query: Annotated[
            str | None,
            Field(description="Only show fields whose name or label contains this text."),
        ] = None,
        include_technical: Annotated[
            bool,
            Field(description="Include chatter/activity/internal fields (message_*, activity_*)."),
        ] = False,
    ) -> dict[str, Any]:
        """Describe a model's fields (type, label, required, readonly, relation,
        selection values) and what the current user is allowed to do with it.

        Required fields are listed first.
        """
        return _guard(service.describe_model, model, field_query, include_technical)

    @mcp.tool(title="Search Odoo records", annotations=read_only)
    def search_records(
        model: Model,
        domain: Domain = None,
        fields: Fields = None,
        limit: Annotated[
            int | None,
            Field(
                description=f"Page size (default {policy.default_limit}, max {policy.max_limit}).",
                ge=1,
            ),
        ] = None,
        offset: Annotated[
            int | None, Field(description="Records to skip, for paging.", ge=0)
        ] = None,
        order: Annotated[
            str | None,
            Field(description="Sort clause, e.g. 'date_order desc, id'."),
        ] = None,
    ) -> dict[str, Any]:
        """Search records matching a domain and return selected fields.

        Returns the total number of matches, the current page of records and,
        when more records exist, `next_offset` for the following page.
        """
        return _guard(service.search_records, model, domain, fields, limit, offset, order)

    @mcp.tool(title="Read one Odoo record", annotations=read_only)
    def read_record(
        model: Model,
        record_id: Annotated[int, Field(description="The record id.", ge=1)],
        fields: Fields = None,
    ) -> dict[str, Any]:
        """Read a single record by id (all non-binary fields unless `fields` is given)."""
        return _guard(service.read_record, model, record_id, fields)

    @mcp.tool(title="Count Odoo records", annotations=read_only)
    def count_records(model: Model, domain: Domain = None) -> dict[str, Any]:
        """Count the records matching a domain (cheap; no data is transferred)."""
        return _guard(service.count_records, model, domain)

    # -- write tools (only registered when enabled) -------------------------

    if settings.allow_write:

        @mcp.tool(
            title="Create an Odoo record",
            annotations=ToolAnnotations(
                read_only_hint=False, destructive_hint=False, open_world_hint=True
            ),
        )
        def create_record(model: Model, values: Values) -> dict[str, Any]:
            """Create a record and return its id and display name.

            Check required fields with describe_model first.
            """
            return _guard(service.create_record, model, values)

        @mcp.tool(
            title="Update Odoo records",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )
        def update_record(model: Model, ids: RecordIds, values: Values) -> dict[str, Any]:
            """Write the same values to one or more existing records.

            Fails without changing anything if any id does not exist.
            """
            return _guard(service.update_record, model, ids, values)

    if settings.allow_delete:

        @mcp.tool(
            title="Delete Odoo records",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        def delete_record(model: Model, ids: RecordIds) -> dict[str, Any]:
            """Permanently delete records. Prefer archiving (active=false) when possible.

            Fails without deleting anything if any id does not exist.
            """
            return _guard(service.delete_record, model, ids)

    # -- resources ----------------------------------------------------------

    @mcp.resource(
        "odoo://server-info",
        name="server-info",
        title="Odoo connection and safety policy",
        description="Odoo version, database, user and the active safety settings.",
        mime_type="application/json",
    )
    def server_info() -> str:
        try:
            return json.dumps(service.server_info(), indent=2)
        except OdooMCPError as err:
            return json.dumps({"error": str(err)}, indent=2)

    @mcp.resource(
        "odoo://models/{model}",
        name="model-schema",
        title="Odoo model schema",
        description="Field definitions of one model (same data as describe_model).",
        mime_type="application/json",
    )
    def model_schema(model: str) -> str:
        return json.dumps(_guard(service.describe_model, model), indent=2)

    # -- prompts ------------------------------------------------------------

    @mcp.prompt(
        title="Sales summary",
        description="Summarise confirmed sales for a period: totals, top customers, top products.",
    )
    def sales_summary(
        period: Annotated[
            str, Field(description="Natural-language period, e.g. 'last month', '2026-Q3'.")
        ] = "this month",
    ) -> str:
        return (
            f"Prepare a concise sales summary for {period} from Odoo.\n\n"
            "1. Convert the period to explicit dates (state them).\n"
            "2. Use count_records and search_records on 'sale.order' with "
            "state in ['sale', 'done'] and date_order inside the period. Request only "
            "the fields you need (name, partner_id, amount_total, currency_id, date_order) "
            "and page through results with next_offset if needed.\n"
            "3. Report: number of orders, total revenue per currency, average order value, "
            "top 5 customers by revenue.\n"
            "4. If 'sale.order.line' is available, list the top 5 products by quantity.\n"
            "5. Finish with 2-3 observations. Do not modify any data."
        )

    @mcp.prompt(
        title="Explore a model",
        description="Guided tour of an Odoo model: purpose, key fields and sample records.",
    )
    def explore_model(
        model: Annotated[str, Field(description="Technical model name, e.g. 'crm.lead'.")],
    ) -> str:
        return (
            f"Help me understand the Odoo model '{model}'.\n\n"
            "1. Call describe_model and explain what the model represents.\n"
            "2. List the required fields and the most important relational fields.\n"
            "3. Call count_records, then search_records with limit 5 to show a sample "
            "in a small table.\n"
            "4. Suggest three useful questions I could ask about this data."
        )

    return mcp


# -- command line -------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="odoo-mcp-server",
        description="MCP server for Odoo. Configure it with ODOO_* environment variables.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Connect to Odoo, print server info and the safety policy, then exit.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio, which is what desktop clients use).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for HTTP transport.")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport.")
    return parser.parse_args(argv)


def create_service(settings: Settings) -> OdooService:
    return OdooService(OdooClient(settings), SafetyPolicy.from_settings(settings))


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")

    try:
        settings = Settings.from_env()
    except ConfigError as err:
        print(f"odoo-mcp-server: configuration error: {err}", file=sys.stderr)
        return 2

    service = create_service(settings)

    if args.check:
        try:
            info = service.server_info()
        except OdooMCPError as err:
            print(f"odoo-mcp-server: connection check failed: {err}", file=sys.stderr)
            return 1
        info["always_blocked_models"] = list(BUILTIN_DENIED_MODELS)
        print(json.dumps(info, indent=2))
        return 0

    log.info(
        "odoo-mcp-server %s -> %s (db=%s, user=%s, mode=%s)",
        __version__,
        settings.url,
        settings.db,
        settings.username,
        settings.mode,
    )
    server = build_server(service)
    if args.transport == "streamable-http":
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            log.warning(
                "HTTP transport bound to %s: this server has no authentication of its own; "
                "anyone who can reach it acts as %s.",
                args.host,
                settings.username,
            )
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
