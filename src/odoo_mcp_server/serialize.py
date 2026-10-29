"""Turn raw Odoo RPC values into clean, JSON-safe structures for the model.

Odoo's wire format has a few quirks that confuse language models:

* empty values are ``False`` for every field type (``"email": False``);
* many2one values are ``[id, "Display Name"]`` pairs;
* binary fields can be megabytes of base64.

This module normalises all of that using the field metadata from
``fields_get`` and, in the other direction, accepts the friendly shapes back
when writing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from .errors import ValidationError, did_you_mean

FieldsMeta = Mapping[str, Mapping[str, Any]]

#: Field types whose ``False`` really means "empty" rather than a boolean.
_X2MANY = {"one2many", "many2many"}
_SCALAR_EMPTY_AS_NONE = {
    "char",
    "text",
    "html",
    "date",
    "datetime",
    "selection",
    "many2one",
    "many2one_reference",
    "reference",
    "binary",
    "image",
    "json",
    "properties",
    "float",
    "monetary",
    "integer",
}


def to_jsonable(value: Any) -> Any:
    """Recursively convert XML-RPC values into JSON-serialisable ones."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<binary: {len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return str(value)


def serialize_value(value: Any, meta: Mapping[str, Any] | None) -> Any:
    """Serialise one field value according to its ``fields_get`` metadata."""
    ftype = (meta or {}).get("type")

    if ftype == "boolean":
        return bool(value)
    if ftype in _X2MANY:
        return list(value) if isinstance(value, (list, tuple)) else []
    if value is False and ftype in _SCALAR_EMPTY_AS_NONE:
        # Integers/floats are never False on the wire, but guard anyway.
        return None
    if ftype == "many2one":
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return {"id": value[0], "name": value[1]}
        if isinstance(value, int):
            return {"id": value, "name": None}
        return None
    if ftype in {"binary", "image"}:
        # Reads are done with bin_size=True, so this is normally a size string.
        if isinstance(value, (bytes, bytearray)):
            return f"<binary: {len(value)} bytes>"
        if isinstance(value, str) and value and not value[0].isdigit():
            return f"<binary: ~{len(value) * 3 // 4} bytes>"
        return f"<binary: {value}>" if value else None
    return to_jsonable(value)


def serialize_record(record: Mapping[str, Any], fields_meta: FieldsMeta) -> dict[str, Any]:
    """Serialise a ``read``/``search_read`` row; ``id`` is always first."""
    out: dict[str, Any] = {}
    if "id" in record:
        out["id"] = record["id"]
    for name, value in record.items():
        if name == "id":
            continue
        out[name] = serialize_value(value, fields_meta.get(name))
    return out


def describe_field(name: str, meta: Mapping[str, Any]) -> dict[str, Any]:
    """Compact description of one field, for ``describe_model``."""
    info: dict[str, Any] = {
        "name": name,
        "label": meta.get("string") or name,
        "type": meta.get("type"),
    }
    if meta.get("required"):
        info["required"] = True
    if meta.get("readonly"):
        info["readonly"] = True
    if meta.get("store") is False:
        info["stored"] = False
    if meta.get("relation"):
        info["relation"] = meta["relation"]
    if meta.get("selection") and isinstance(meta["selection"], (list, tuple)):
        info["selection"] = [
            {"value": item[0], "label": item[1]}
            for item in meta["selection"]
            if isinstance(item, (list, tuple)) and len(item) == 2
        ]
    if meta.get("help"):
        info["help"] = meta["help"]
    return info


def prepare_values(
    values: Mapping[str, Any], fields_meta: FieldsMeta, model: str
) -> dict[str, Any]:
    """Validate and convert friendly write values into Odoo's wire format.

    * unknown fields are rejected (with suggestions);
    * a many2one given as ``{"id": 7, "name": ...}`` becomes ``7``;
    * an x2many given as a plain list of ids becomes ``[(6, 0, ids)]``
      (replace the whole set); explicit Odoo command lists are passed through;
    * ``None`` becomes ``False``, which is how Odoo clears a field.
    """
    if not isinstance(values, Mapping) or not values:
        raise ValidationError("'values' must be a non-empty object of field: value pairs.")

    prepared: dict[str, Any] = {}
    for name, value in values.items():
        meta = fields_meta.get(name)
        if meta is None:
            raise ValidationError(
                f"Field '{name}' does not exist on '{model}'." + did_you_mean(name, fields_meta)
            )
        if name == "id" or meta.get("type") in {"binary", "image"}:
            raise ValidationError(f"Field '{name}' cannot be written through this server.")
        if meta.get("readonly") and meta.get("store") is False:
            raise ValidationError(f"Field '{name}' on '{model}' is computed and read-only.")
        prepared[name] = _prepare_value(name, value, meta)
    return prepared


def _prepare_value(name: str, value: Any, meta: Mapping[str, Any]) -> Any:
    ftype = meta.get("type")
    if value is None:
        return False
    if ftype == "many2one":
        if isinstance(value, Mapping):
            value = value.get("id")
        if value is False:
            return False
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(
                f"Field '{name}' is a many2one to '{meta.get('relation')}': pass a record id."
            )
        return value
    if ftype in _X2MANY:
        if not isinstance(value, (list, tuple)):
            raise ValidationError(f"Field '{name}' expects a list of ids or Odoo commands.")
        if all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return [(6, 0, list(value))]
        if all(isinstance(v, Mapping) and isinstance(v.get("id"), int) for v in value):
            return [(6, 0, [v["id"] for v in value])]
        if all(isinstance(v, (list, tuple)) and v and isinstance(v[0], int) for v in value):
            return [list(cmd) for cmd in value]
        raise ValidationError(
            f"Field '{name}' expects a list of ids, e.g. [1, 2], or Odoo commands "
            "such as [[4, 7, 0]]."
        )
    if ftype == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"Field '{name}' is a boolean: pass true or false.")
        return value
    if ftype == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValidationError(f"Field '{name}' is an integer.")
    if ftype in {"float", "monetary"} and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        raise ValidationError(f"Field '{name}' is a number.")
    if ftype == "selection" and isinstance(meta.get("selection"), (list, tuple)):
        allowed = [item[0] for item in meta["selection"]]
        if allowed and value not in allowed:
            raise ValidationError(
                f"Invalid value {value!r} for '{name}'. Allowed: {', '.join(map(str, allowed))}."
            )
    return value
