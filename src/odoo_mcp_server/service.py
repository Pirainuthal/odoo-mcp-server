"""High-level Odoo operations used by the MCP tools.

This layer is independent of the MCP SDK: it combines the RPC client, the
safety policy and the serializer, and returns plain JSON-safe dicts. Keeping it
SDK-free makes it easy to unit-test and to reuse from scripts.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from .client import OdooClient
from .errors import OdooRPCError, SafetyError, ValidationError, did_you_mean
from .safety import SafetyPolicy
from .serialize import (
    describe_field,
    prepare_values,
    serialize_record,
)

#: Attributes requested from ``fields_get`` (keeps payloads small).
FIELD_ATTRIBUTES = [
    "string",
    "type",
    "required",
    "readonly",
    "relation",
    "selection",
    "help",
    "store",
]

#: Fields picked, when present, for the compact default projection of a search.
COMMON_FIELDS = (
    "name",
    "display_name",
    "ref",
    "default_code",
    "state",
    "date",
    "date_order",
    "invoice_date",
    "partner_id",
    "user_id",
    "email",
    "phone",
    "list_price",
    "amount_total",
    "amount_residual",
    "currency_id",
    "company_id",
    "active",
)
_DEFAULT_FIELDS_CAP = 15
_TECHNICAL_PREFIXES = ("message_", "activity_", "website_message_", "__")
_BINARY_TYPES = {"binary", "image"}
_READ_CONTEXT = {"bin_size": True}
_ACCESS_OPERATIONS = ("read", "create", "write", "unlink")
_X2MANY = {"one2many", "many2many"}
_SUBDOMAIN_OPERATORS = {"any", "not any"}
#: Odoo x2many command codes (``odoo.fields.Command``).
_CREATE, _UPDATE, _DELETE, _UNLINK, _LINK, _CLEAR, _SET = range(7)
#: On an existing record, these one2many commands delete the lines they detach
#: when the inverse many2one is ``ondelete='cascade'`` (e.g. order lines).
_O2M_DETACH = {_UNLINK, _CLEAR, _SET}


def _all_fields(meta: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Every field except binaries and chatter/technical ones."""
    return [
        name
        for name, info in meta.items()
        if info.get("type") not in _BINARY_TYPES and not name.startswith(_TECHNICAL_PREFIXES)
    ]


class OdooService:
    """Safe, model-friendly operations on one Odoo database."""

    def __init__(self, client: OdooClient, policy: SafetyPolicy) -> None:
        self.client = client
        self.policy = policy
        self._fields_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    # -- metadata ---------------------------------------------------------

    def fields_meta(self, model: str) -> dict[str, dict[str, Any]]:
        """``fields_get`` for ``model``, cached for the life of the process."""
        with self._cache_lock:
            cached = self._fields_cache.get(model)
        if cached is not None:
            return cached
        try:
            meta = self.client.execute(model, "fields_get", attributes=FIELD_ATTRIBUTES)
        except OdooRPCError as err:
            if "doesn't exist" in str(err) or "KeyError" in str(err):
                raise ValidationError(
                    f"Model '{model}' does not exist (or its module is not installed). "
                    "Use list_models to find the right technical name."
                ) from None
            raise
        meta.setdefault("id", {"string": "ID", "type": "integer", "readonly": True})
        with self._cache_lock:
            self._fields_cache[model] = meta
        return meta

    def _default_fields(self, meta: Mapping[str, Mapping[str, Any]]) -> list[str]:
        """A compact, useful projection when the caller does not pass ``fields``."""
        chosen = [name for name in COMMON_FIELDS if name in meta]
        for name, info in meta.items():
            if len(chosen) >= _DEFAULT_FIELDS_CAP:
                break
            if (
                info.get("required")
                and name not in chosen
                and info.get("type") not in _BINARY_TYPES | {"one2many", "many2many"}
                and not name.startswith(_TECHNICAL_PREFIXES)
                and name not in {"create_uid", "write_uid", "create_date", "write_date"}
            ):
                chosen.append(name)
        return chosen or ["display_name"]

    def _resolve_fields(
        self, model: str, fields: Sequence[str] | None, meta: Mapping[str, Any]
    ) -> list[str]:
        if not fields:
            return self._default_fields(meta)
        if list(fields) in (["*"], ["__all__"]):
            return _all_fields(meta)
        return self.policy.check_fields(fields, meta, model)

    # -- discovery --------------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """Connection details and the active safety policy (no secrets)."""
        version = self.client.version()
        s = self.client.settings
        p = self.policy
        return {
            "url": s.url,
            "database": s.db,
            "username": s.username,
            "uid": self.client.uid,
            "server_version": version.get("server_version"),
            "server_serie": version.get("server_serie"),
            "mode": s.mode,
            "allowed_models": list(p.allowed_models) or "all (except denied)",
            "denied_models": list(p.denied_models),
            "default_limit": p.default_limit,
            "max_limit": p.max_limit,
        }

    def list_models(
        self,
        query: str | None = None,
        include_transient: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        domain: list[Any] = []
        if query and query.strip():
            q = query.strip()
            domain = ["|", ["model", "ilike", q], ["name", "ilike", q]]
        if not include_transient:
            domain.append(["transient", "=", False])
        rows = self.client.execute(
            "ir.model", "search_read", domain, fields=["model", "name", "transient"], order="model"
        )
        allowed = [row for row in rows if self.policy.is_model_allowed(row["model"])]
        cap = self.policy.clamp_limit(limit)
        models = [
            {
                "model": row["model"],
                "name": row["name"],
                **({"transient": True} if row.get("transient") else {}),
            }
            for row in allowed[:cap]
        ]
        return {"total": len(allowed), "returned": len(models), "models": models}

    def describe_model(
        self,
        model: str,
        field_query: str | None = None,
        include_technical: bool = False,
    ) -> dict[str, Any]:
        model = self.policy.check_model(model)
        meta = self.fields_meta(model)
        q = (field_query or "").strip().lower()
        fields = []
        for name in sorted(meta, key=lambda n: (not meta[n].get("required"), n)):
            info = meta[name]
            if not include_technical and name.startswith(_TECHNICAL_PREFIXES):
                continue
            if q and q not in name.lower() and q not in str(info.get("string", "")).lower():
                continue
            field = describe_field(name, info)
            if field.get("relation") and not self.policy.is_model_allowed(field["relation"]):
                field["relation_blocked"] = True
            fields.append(field)
        return {
            "model": model,
            "field_count": len(fields),
            "access": self._access_rights(model),
            "fields": fields,
        }

    def _access_rights(self, model: str) -> dict[str, bool | None]:
        """What the API-key user may do on ``model`` (``None`` if unknown)."""
        rights: dict[str, bool | None] = {}
        for op in _ACCESS_OPERATIONS:
            try:
                rights[op] = bool(self.client.execute(model, "has_access", [], op))
            except OdooRPCError:
                try:  # Odoo <= 17
                    rights[op] = bool(
                        self.client.execute(model, "check_access_rights", op, raise_exception=False)
                    )
                except OdooRPCError:
                    rights[op] = None
        if not self.policy.allow_write:
            rights["create"] = rights["write"] = False
        if not self.policy.allow_delete:
            rights["unlink"] = False
        return rights

    # -- reading ----------------------------------------------------------

    def search_records(
        self,
        model: str,
        domain: Any = None,
        fields: Sequence[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        model = self.policy.check_model(model)
        meta = self.fields_meta(model)
        clean_domain = self._check_domain(model, meta, domain)
        field_names = self._resolve_fields(model, fields, meta)
        order = self.policy.check_order(order, meta, model)
        limit_ = self.policy.clamp_limit(limit)
        offset_ = self.policy.check_offset(offset)

        rows = self.client.execute(
            model,
            "search_read",
            clean_domain,
            fields=field_names,
            limit=limit_,
            offset=offset_,
            order=order,
            context=_READ_CONTEXT,
        )
        total = self.client.execute(model, "search_count", clean_domain)
        records = [serialize_record(row, meta) for row in rows]
        result: dict[str, Any] = {
            "model": model,
            "total": total,
            "offset": offset_,
            "limit": limit_,
            "returned": len(records),
            "records": records,
        }
        if offset_ + len(records) < total:
            result["next_offset"] = offset_ + len(records)
        if limit is not None and limit > limit_:
            result["note"] = f"limit was capped at {limit_} (ODOO_MCP_MAX_LIMIT)."
        return result

    def read_record(
        self, model: str, record_id: int, fields: Sequence[str] | None = None
    ) -> dict[str, Any]:
        model = self.policy.check_model(model)
        if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
            raise ValidationError("'record_id' must be a positive integer.")
        meta = self.fields_meta(model)
        field_names = self._resolve_fields(model, fields, meta) if fields else _all_fields(meta)
        # search_read instead of read: a missing/inaccessible id yields [] instead of a fault.
        rows = self.client.execute(
            model,
            "search_read",
            [["id", "=", record_id]],
            fields=field_names,
            limit=1,
            context=dict(_READ_CONTEXT, active_test=False),
        )
        if not rows:
            raise OdooRPCError(
                f"No '{model}' record with id {record_id} (it may not exist or you may not "
                "have access to it).",
                "missing",
            )
        return {"model": model, "record": serialize_record(rows[0], meta)}

    def count_records(self, model: str, domain: Any = None) -> dict[str, Any]:
        model = self.policy.check_model(model)
        meta = self.fields_meta(model)
        clean_domain = self._check_domain(model, meta, domain)
        count = self.client.execute(model, "search_count", clean_domain)
        return {"model": model, "count": count}

    # -- writing ----------------------------------------------------------

    def create_record(self, model: str, values: Mapping[str, Any]) -> dict[str, Any]:
        self.policy.check_write()
        model = self.policy.check_model(model)
        meta = self.fields_meta(model)
        prepared = prepare_values(values, meta, model)
        self._check_relational_values(model, meta, prepared, creating=True)
        new_id = self.client.execute(model, "create", prepared)
        if isinstance(new_id, list):  # create() is batched since Odoo 17 for list input
            new_id = new_id[0]
        name = self._display_names(model, [new_id]).get(new_id)
        return {"model": model, "id": new_id, "display_name": name, "created": True}

    def update_record(
        self, model: str, ids: Sequence[int] | int, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.policy.check_write()
        model = self.policy.check_model(model)
        clean_ids = self.policy.check_ids(ids)
        meta = self.fields_meta(model)
        prepared = prepare_values(values, meta, model)
        self._check_relational_values(model, meta, prepared, creating=False)
        self._ensure_exist(model, clean_ids)
        self.client.execute(model, "write", clean_ids, prepared)
        return {
            "model": model,
            "updated_ids": clean_ids,
            "fields": sorted(prepared),
        }

    def delete_record(self, model: str, ids: Sequence[int] | int) -> dict[str, Any]:
        self.policy.check_delete()
        model = self.policy.check_model(model)
        clean_ids = self.policy.check_ids(ids)
        names = self._ensure_exist(model, clean_ids)
        self.client.execute(model, "unlink", clean_ids)
        return {
            "model": model,
            "deleted": [{"id": i, "display_name": names.get(i)} for i in clean_ids],
        }

    # -- relational safety --------------------------------------------------

    def _check_domain(self, model: str, meta: Mapping[str, Any], domain: Any) -> list[Any]:
        """Validate a domain's shape, then every field path in it (see :meth:`_check_paths`)."""
        clean = self.policy.parse_domain(domain, meta, model)
        self._check_paths(model, meta, clean)
        return clean

    def _check_paths(self, model: str, meta: Mapping[str, Any], domain: list[Any]) -> None:
        """Check each condition's dotted path hop by hop.

        Every segment must exist on its model, and a path may not travel through
        a relation into a model blocked by the policy - otherwise a condition
        such as ``["api_key_ids.name", "ilike", "x"]`` would query a blocked
        model indirectly. ``any`` sub-domains are checked against the comodel.
        """
        for leaf in domain:
            if not isinstance(leaf, list) or leaf in ([1, "=", 1], [0, "=", 1]):
                continue
            path, operator, value = leaf
            target, info = self._walk_path(model, meta, path, context="domain")
            if info is None:  # inside a properties field: Odoo validates the rest
                continue
            relation = info.get("relation")
            if operator in _SUBDOMAIN_OPERATORS:
                comodel = relation or (target if path.rsplit(".", 1)[-1] == "id" else None)
                if not comodel:
                    raise ValidationError(
                        f"Operator '{operator}' needs a relational field, but '{path}' is "
                        f"a {info.get('type')} field."
                    )
                self._ensure_relation_allowed(comodel, path)
                self._check_paths(comodel, self.fields_meta(comodel), value)
            elif relation and not self.policy.is_model_allowed(relation):
                # Comparing ids is harmless; text matches would search the comodel.
                values = value if isinstance(value, list) else [value]
                if not all(v is None or isinstance(v, (bool, int)) for v in values):
                    self._ensure_relation_allowed(relation, path)

    def _walk_path(
        self, model: str, meta: Mapping[str, Any], path: str, context: str = "domain"
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Follow ``a.b.c`` from ``model``; return the last model and field info."""
        segments = path.split(".")
        current_model, current_meta = model, meta
        for index, name in enumerate(segments):
            info = current_meta.get(name)
            if info is None:
                raise ValidationError(
                    f"Unknown field '{name}' on '{current_model}' in {context} path "
                    f"'{path}'." + did_you_mean(name, current_meta)
                )
            if index == len(segments) - 1:
                return current_model, info
            if info.get("type") == "properties":
                return current_model, None
            relation = info.get("relation")
            if not relation:
                raise ValidationError(
                    f"'{name}' on '{current_model}' is not a relational field, so "
                    f"'{path}' cannot continue past it."
                )
            self._ensure_relation_allowed(relation, path)
            current_model, current_meta = relation, self.fields_meta(relation)
        raise AssertionError("unreachable")  # pragma: no cover

    def _ensure_relation_allowed(self, comodel: str, path: str) -> None:
        reason = self.policy.model_denial_reason(comodel)
        if reason:
            raise SafetyError(f"'{path}' reaches a blocked model. {reason}")

    def _check_relational_values(
        self,
        model: str,
        meta: Mapping[str, Any],
        values: Mapping[str, Any],
        *,
        creating: bool,
    ) -> None:
        """Apply the safety policy to x2many commands, recursively.

        * create/update commands (0, 1) write to the comodel, so the comodel
          must be allowed; their nested values are checked the same way;
        * delete (2) always needs ``ODOO_MCP_ALLOW_DELETE``;
        * on an existing record, detaching one2many lines (3, 5, 6 - which is
          what a plain list of ids becomes) can delete them, so it needs
          ``ODOO_MCP_ALLOW_DELETE`` too.
        """
        for name, value in values.items():
            info = meta.get(name) or {}
            ftype = info.get("type")
            if ftype not in _X2MANY or not isinstance(value, (list, tuple)):
                continue
            comodel = info.get("relation") or ""
            for command in value:
                if not isinstance(command, (list, tuple)) or not command:
                    raise ValidationError(f"Invalid command {command!r} for field '{name}'.")
                code = command[0]
                if isinstance(code, bool) or code not in range(7):
                    raise ValidationError(
                        f"Unknown x2many command {code!r} for field '{name}' (valid codes are 0-6)."
                    )
                if code in (_CREATE, _UPDATE):
                    if len(command) != 3 or not isinstance(command[2], Mapping):
                        raise ValidationError(
                            f"Command {code} for field '{name}' must be [{code}, id, {{values}}]."
                        )
                    self._ensure_relation_allowed(comodel, f"{model}.{name}")
                    self._check_relational_values(
                        comodel,
                        self.fields_meta(comodel),
                        command[2],
                        creating=code == _CREATE,
                    )
                elif code == _DELETE and not self.policy.allow_delete:
                    raise SafetyError(
                        f"Command 2 on '{name}' deletes '{comodel}' records; that needs "
                        "ODOO_MCP_ALLOW_DELETE=1. Use [3, id] to only remove the link."
                    )
                elif (
                    ftype == "one2many"
                    and not creating
                    and code in _O2M_DETACH
                    and not self.policy.allow_delete
                ):
                    raise SafetyError(
                        f"Removing or replacing lines of one2many '{name}' can delete them in "
                        "Odoo, so it needs ODOO_MCP_ALLOW_DELETE=1. To add lines without "
                        "removing others, use [[0, 0, {...}]] (new) or [[4, id]] (link)."
                    )

    # -- helpers ----------------------------------------------------------

    def _display_names(self, model: str, ids: list[int]) -> dict[int, str]:
        rows = self.client.execute(
            model,
            "search_read",
            [["id", "in", ids]],
            fields=["display_name"],
            context={"active_test": False},
        )
        return {row["id"]: row.get("display_name") for row in rows}

    def _ensure_exist(self, model: str, ids: list[int]) -> dict[int, str]:
        names = self._display_names(model, ids)
        missing = [i for i in ids if i not in names]
        if missing:
            raise OdooRPCError(
                f"No '{model}' record(s) with id(s) {missing} (missing or not accessible). "
                "Nothing was changed.",
                "missing",
            )
        return names
