"""The safety policy: what the AI assistant is and is not allowed to do.

Layers, from outermost to innermost:

1. **Mode** - read-only by default; writes and deletes are separate opt-ins.
2. **Model policy** - a built-in denylist of security-sensitive models that can
   never be reached, an optional extra denylist, and an optional allowlist.
3. **Argument validation** - domains, field names, ``order`` clauses and limits
   are checked before anything is sent to Odoo.
4. **Odoo itself** - every call runs as the API-key user, so access rights and
   record rules still apply.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from .config import Settings
from .errors import SafetyError, ValidationError, did_you_mean

#: Models that expose credentials, security configuration or code execution.
#: These are refused regardless of configuration.
BUILTIN_DENIED_MODELS: tuple[str, ...] = (
    "res.users.apikeys",
    "res.users.apikeys.*",
    "res.users.identitycheck",
    "res.users.deletion",
    "auth_totp.device",
    "ir.config_parameter",
    "ir.rule",
    "ir.model.access",
    "ir.model.fields",
    "ir.model.fields.*",
    "ir.actions.server",
    "base.automation",
    "ir.cron",
    "ir.cron.*",
    "ir.mail_server",
    "ir.module.*",
    "ir.ui.view",
    "ir.asset",
    "fetchmail.server",
    "payment.provider",
    "base.module.*",
)

#: Operators accepted in domain leaves (Odoo 17-19).
DOMAIN_OPERATORS = frozenset(
    {
        "=",
        "!=",
        "<>",
        ">",
        ">=",
        "<",
        "<=",
        "=?",
        "like",
        "not like",
        "ilike",
        "not ilike",
        "=like",
        "=ilike",
        "not =like",
        "not =ilike",
        "in",
        "not in",
        "child_of",
        "parent_of",
        "any",
        "not any",
    }
)
_LOGIC_OPERATORS = {"&": 2, "|": 2, "!": 1}
_FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_MODEL_NAME = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_ORDER_TERM = re.compile(
    r"^(?P<field>[A-Za-z_][A-Za-z0-9_.]*)(\s+(asc|desc))?(\s+nulls\s+(first|last))?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafetyPolicy:
    """Stateless checks derived from :class:`~odoo_mcp_server.config.Settings`."""

    allow_write: bool = False
    allow_delete: bool = False
    allowed_models: tuple[str, ...] = ()
    denied_models: tuple[str, ...] = ()
    default_limit: int = 50
    max_limit: int = 200
    max_write_batch: int = 100

    @classmethod
    def from_settings(cls, settings: Settings) -> SafetyPolicy:
        return cls(
            allow_write=settings.allow_write,
            allow_delete=settings.allow_delete,
            allowed_models=settings.allowed_models,
            denied_models=settings.denied_models,
            default_limit=settings.default_limit,
            max_limit=settings.max_limit,
        )

    # -- models -----------------------------------------------------------

    def model_denial_reason(self, model: str) -> str | None:
        """Return why ``model`` is not accessible, or ``None`` if it is."""
        if any(fnmatchcase(model, pattern) for pattern in BUILTIN_DENIED_MODELS):
            return f"Model '{model}' is security-sensitive and always blocked by this server."
        if any(fnmatchcase(model, pattern) for pattern in self.denied_models):
            return f"Model '{model}' is blocked by ODOO_MCP_DENY_MODELS."
        if self.allowed_models and not any(
            fnmatchcase(model, pattern) for pattern in self.allowed_models
        ):
            return (
                f"Model '{model}' is not in the ODOO_MCP_MODELS allowlist "
                f"({', '.join(self.allowed_models)})."
            )
        return None

    def is_model_allowed(self, model: str) -> bool:
        return self.model_denial_reason(model) is None

    def check_model(self, model: str) -> str:
        """Validate a model name and return it normalised."""
        if not isinstance(model, str) or not model.strip():
            raise ValidationError("'model' must be a technical model name such as 'res.partner'.")
        model = model.strip()
        if not _MODEL_NAME.match(model):
            raise ValidationError(
                f"'{model}' is not a valid technical model name (expected e.g. 'sale.order')."
            )
        reason = self.model_denial_reason(model)
        if reason:
            raise SafetyError(reason)
        return model

    # -- mode -------------------------------------------------------------

    def check_write(self) -> None:
        if not self.allow_write:
            raise SafetyError(
                "This server is read-only. Set ODOO_MCP_ALLOW_WRITE=1 to allow create/update."
            )

    def check_delete(self) -> None:
        if not self.allow_delete:
            raise SafetyError(
                "Deleting records is disabled. Set ODOO_MCP_ALLOW_WRITE=1 and "
                "ODOO_MCP_ALLOW_DELETE=1 to allow it."
            )

    # -- paging -----------------------------------------------------------

    def clamp_limit(self, limit: int | None) -> int:
        """Apply the default limit and the hard maximum."""
        if limit is None:
            return self.default_limit
        if limit < 1:
            raise ValidationError("'limit' must be a positive integer.")
        return min(limit, self.max_limit)

    @staticmethod
    def check_offset(offset: int | None) -> int:
        if offset is None:
            return 0
        if offset < 0:
            raise ValidationError("'offset' must be >= 0.")
        return offset

    def check_ids(self, ids: Sequence[int] | int) -> list[int]:
        if isinstance(ids, int) and not isinstance(ids, bool):
            ids = [ids]
        if not isinstance(ids, (list, tuple)) or not ids:
            raise ValidationError("'ids' must be a non-empty list of record ids.")
        clean: list[int] = []
        for value in ids:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"Invalid record id {value!r}: ids are positive integers.")
            if value not in clean:
                clean.append(value)
        if len(clean) > self.max_write_batch:
            raise SafetyError(
                f"Refusing to modify {len(clean)} records at once "
                f"(maximum {self.max_write_batch} per call)."
            )
        return clean

    # -- fields / order ---------------------------------------------------

    @staticmethod
    def check_fields(fields: Sequence[str], known: Iterable[str], model: str) -> list[str]:
        known_set = set(known)
        result: list[str] = []
        for name in fields:
            if not isinstance(name, str) or not name:
                raise ValidationError(f"Invalid field name {name!r}.")
            if name not in known_set:
                raise ValidationError(
                    f"Field '{name}' does not exist on '{model}'." + did_you_mean(name, known_set)
                )
            if name not in result:
                result.append(name)
        return result

    @staticmethod
    def check_order(order: str | None, known: Iterable[str], model: str) -> str | None:
        """Validate a comma-separated ``order`` clause.

        When ``known`` is the ``fields_get`` mapping, a dotted term is only
        accepted where Odoo supports one: ``many2one_field.id`` or a key of a
        properties field. Odoo does not sort by a related model's field.
        """
        if order is None or not order.strip():
            return None
        known_set = set(known)
        types = (
            {name: info.get("type") for name, info in known.items()}
            if isinstance(known, Mapping)
            else {}
        )
        terms = []
        for raw in order.split(","):
            term = " ".join(raw.split())
            match = _ORDER_TERM.match(term)
            if not match:
                raise ValidationError(
                    f"Invalid order clause {raw.strip()!r}. Use 'field [asc|desc]', "
                    "comma-separated, e.g. 'date_order desc, id'."
                )
            root = match["field"].split(".", 1)[0]
            if root not in known_set:
                raise ValidationError(
                    f"Cannot order by '{root}': no such field on '{model}'."
                    + did_you_mean(root, known_set)
                )
            path = match["field"]
            if "." in path and types:
                suffix = path.split(".", 1)[1]
                ok = types.get(root) == "properties" or (
                    types.get(root) == "many2one" and suffix == "id"
                )
                if not ok:
                    raise ValidationError(
                        f"Cannot order by '{path}': Odoo does not sort by a related "
                        f"model's field. Use '{root}' (sorts by the related model's "
                        f"default order) or '{root}.id'."
                    )
            terms.append(term)
        return ", ".join(terms)

    # -- domains ----------------------------------------------------------

    def parse_domain(
        self, domain: Any, known: Iterable[str] | None = None, model: str = ""
    ) -> list[Any]:
        """Validate an Odoo domain and return it as a list.

        Accepts a list (or a JSON string of a list). Checks leaf shape,
        operators, field names (first path segment against ``known``),
        nested ``any`` domains and prefix-notation arity.
        """
        if domain is None:
            return []
        if isinstance(domain, str):
            try:
                domain = json.loads(domain) if domain.strip() else []
            except json.JSONDecodeError:
                raise ValidationError(
                    "'domain' must be a JSON list such as "
                    '[["is_company", "=", true], ["name", "ilike", "acme"]].'
                ) from None
        if not isinstance(domain, (list, tuple)):
            raise ValidationError("'domain' must be a list of conditions.")
        known_set = set(known) if known is not None else None
        return self._check_domain(list(domain), known_set, model)

    def _check_domain(self, domain: list[Any], known: set[str] | None, model: str) -> list[Any]:
        out: list[Any] = []
        for token in domain:
            if isinstance(token, str):
                if token not in _LOGIC_OPERATORS:
                    raise ValidationError(
                        f"Unexpected domain token {token!r}: only '&', '|' and '!' "
                        "may appear between conditions."
                    )
                out.append(token)
            elif isinstance(token, (list, tuple)):
                out.append(self._check_leaf(list(token), known, model))
            elif token is True or token is False:
                # Odoo's TRUE_LEAF / FALSE_LEAF shorthand.
                out.append([1, "=", 1] if token else [0, "=", 1])
            else:
                raise ValidationError(f"Invalid domain element {token!r}.")
        _check_arity(out)
        return out

    def _check_leaf(self, leaf: list[Any], known: set[str] | None, model: str) -> list[Any]:
        if len(leaf) != 3:
            raise ValidationError(
                f"Invalid condition {leaf!r}: each condition is [field, operator, value]."
            )
        field_path, operator, value = leaf
        if leaf in ([1, "=", 1], [0, "=", 1]):
            return leaf
        if not isinstance(field_path, str) or not _FIELD_PATH.match(field_path):
            raise ValidationError(f"Invalid field path {field_path!r} in condition {leaf!r}.")
        if not isinstance(operator, str) or operator.lower() not in DOMAIN_OPERATORS:
            allowed = ", ".join(sorted(DOMAIN_OPERATORS))
            raise ValidationError(f"Unsupported operator {operator!r}. Allowed: {allowed}.")
        operator = operator.lower()
        if operator == "<>":  # deprecated alias, removed from Odoo's docs since 19.0
            operator = "!="
        root = field_path.split(".", 1)[0]
        if known is not None and root not in known:
            raise ValidationError(
                f"Unknown field '{root}' in domain for '{model}'." + did_you_mean(root, known)
            )
        if operator in {"any", "not any"}:
            if not isinstance(value, (list, tuple)):
                raise ValidationError(f"Operator '{operator}' expects a sub-domain list as value.")
            # The sub-domain targets a related model whose fields we don't know here.
            value = self._check_domain(list(value), None, model)
        elif operator in {"in", "not in"}:
            if not isinstance(value, (list, tuple)):
                value = [value]
            value = list(value)
            _check_json_scalars(value, leaf)
        else:
            _check_json_scalars([value] if not isinstance(value, (list, tuple)) else value, leaf)
        return [field_path, operator, value]


def _check_json_scalars(values: Iterable[Any], leaf: list[Any]) -> None:
    for item in values:
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValidationError(f"Unsupported value {item!r} in condition {leaf!r}.")


def _check_arity(domain: list[Any]) -> None:
    """Verify prefix (Polish) notation: every '&'/'|' needs two operands, '!' one."""
    stack = 0
    for token in reversed(domain):
        if isinstance(token, str):
            needed = _LOGIC_OPERATORS[token]
            if stack < needed:
                raise ValidationError(
                    f"Malformed domain: operator {token!r} needs {needed} condition(s) after it."
                )
            stack -= needed - 1
        else:
            stack += 1
