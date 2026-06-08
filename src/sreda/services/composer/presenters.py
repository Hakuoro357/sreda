"""Deterministic per-tool "display_text" presenters for the planner composer (#110).

The planner must NOT hand-pick output-model field names for narration (that was the
root of the `compose_ref_unknown_field` class). Instead the composer builds the
user-facing summary in code, here.

Public entry points:

* :func:`render_display_text` — for an ``ok`` step: a public-safe, non-empty,
  length-capped string from the tool's result. **The result is the executor's
  ``StepResult.parsed_output``, i.e. a plain ``dict``** (the model was already
  ``model_dump``-ed in the executor), so we resolve the safe field via a
  precomputed map, NOT by introspecting the runtime value's type.
* :func:`render_execution_failure` — for an executor-level failure
  (``StepResult.status != "ok"`` and not ``skipped``): a fixed public-safe phrase
  that never leaks internal error text.

**display_field convention (annotation-driven, plan choice X).** A tool output-model
variant declares the single SAFE public field to display via a class attribute::

    class WebSearchToolOk(BaseModel):
        __display_field__: ClassVar[str | None] = "raw_text"
        status: Literal["results"] = "results"
        raw_text: str

At startup :func:`build_display_field_map` introspects every ToolSpec's output_model
union, reading each variant's ``__display_field__`` + its ``status`` literal(s), into
a ``(tool_name, domain_status) -> field`` map. ``render_display_text`` consumes that
map plus the runtime ``dict``. Variants WITHOUT the annotation → deny-by-default
(a fixed safe phrase + a fallback metric) — we never guess a field. A small override
registry handles the few tools whose narration is more than one literal field.

All functions are pure (no I/O, DB, or network).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Callable, Iterable, Mapping, get_args, get_origin

logger = logging.getLogger(__name__)

DISPLAY_TEXT_MAX_CHARS = 2000

_DENY_BY_DEFAULT = "Не могу безопасно показать результат."
_EMPTY_DISPLAY = "Нет данных для показа."

# Observability: deny-by-default fires keyed by (tool_name, domain_status). A non-empty
# dict in telemetry means a narratable (tool, status) has no display_field/override yet.
PRESENTER_FALLBACK_COUNTS: dict[tuple[str, str], int] = {}

_EXECUTION_FAILURE_PHRASES: dict[str, str] = {
    "error": "Не получилось выполнить действие. Попробуй ещё раз позже.",
    "timeout": "Действие заняло слишком много времени. Попробуй ещё раз.",
    "plan_gap": "Не получилось корректно подготовить действие. Попробуй переформулировать.",
    "arg_violation": "Не получилось выполнить действие из-за неверных параметров.",
    "unknown_outcome": "Действие выполнилось, но результат не удалось безопасно распознать.",
}
_EXECUTION_FAILURE_DEFAULT = _EXECUTION_FAILURE_PHRASES["error"]


# ---------------------------------------------------------------------------
# coercion / formatting helpers
# ---------------------------------------------------------------------------


def _as_mapping(output: Any) -> Mapping[str, Any] | None:
    """Read-only mapping view — accepts a plain dict OR (defensively) a pydantic
    BaseModel instance. Production passes a dict; the model path is belt-and-braces."""
    if isinstance(output, Mapping):
        return output
    model_dump = getattr(output, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _coerce_to_str(value: Any) -> str:
    if value is None:
        return _EMPTY_DISPLAY
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    if isinstance(value, (list, tuple)):
        return "\n".join(_coerce_to_str(item) for item in value if item is not None)
    return str(value)


def _cap(text: str) -> str:
    if len(text) > DISPLAY_TEXT_MAX_CHARS:
        return text[: max(0, DISPLAY_TEXT_MAX_CHARS - 3)] + "..."
    return text


def _finalize(value: Any) -> str:
    """Coerce → guarantee non-empty → cap."""
    text = _coerce_to_str(value)
    if not text:
        text = _EMPTY_DISPLAY
    return _cap(text)


# ---------------------------------------------------------------------------
# display_field map — built from ToolSpec output_model variant annotations
# ---------------------------------------------------------------------------


def _union_members(output_model: Any) -> tuple[type, ...]:
    """Variant classes of an output_model.

    Handles ``Annotated[Union[A, B, ...], Field(discriminator="status")]``, a bare
    ``Union``/``A | B``, and a single model. (Mirrors validator's union introspection;
    kept minimal here — unify later if it drifts.)"""
    inner = output_model
    if get_origin(output_model) is Annotated:  # unwrap Annotated[X, meta...] → X
        inner = get_args(output_model)[0]
    members = get_args(inner)  # Union[A, B, ...] → (A, B, ...); bare model → ()
    if not members:
        members = (inner,)
    return tuple(m for m in members if isinstance(m, type))


def _status_literals(variant: type) -> tuple[str, ...]:
    """The literal value(s) of the variant's ``status`` field (its discriminator)."""
    fields = getattr(variant, "model_fields", None)
    if not fields or "status" not in fields:
        return ()
    lits = get_args(fields["status"].annotation)
    return tuple(str(v) for v in lits)


def build_display_field_map(specs: Iterable[Any]) -> dict[tuple[str, str], str]:
    """Introspect ToolSpecs → ``(tool_name, domain_status) -> display_field``.

    ``specs`` are objects with ``.name`` and ``.output_model`` (real ToolSpec, or any
    duck-typed stand-in in tests). Only variants that declare ``__display_field__``
    contribute; everything else stays deny-by-default by omission.
    """
    mapping: dict[tuple[str, str], str] = {}
    for spec in specs:
        name = getattr(spec, "name", None)
        output_model = getattr(spec, "output_model", None)
        if not isinstance(name, str) or output_model is None:
            continue
        for variant in _union_members(output_model):
            field = getattr(variant, "__display_field__", None)
            if not (isinstance(field, str) and field):  # ignore missing/empty annotation
                continue
            for status_value in _status_literals(variant):
                mapping[(name, status_value)] = field
    return mapping


# Lazily built from the live registry on first use; tests inject via
# :func:`set_display_field_map`.
_DISPLAY_FIELD_MAP: dict[tuple[str, str], str] | None = None


def set_display_field_map(mapping: dict[tuple[str, str], str]) -> None:
    """Install the (tool, status) → field map (app startup or tests)."""
    global _DISPLAY_FIELD_MAP
    _DISPLAY_FIELD_MAP = dict(mapping)


def _display_field_map() -> dict[tuple[str, str], str]:
    global _DISPLAY_FIELD_MAP
    if _DISPLAY_FIELD_MAP is None:
        try:
            from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS  # lazy

            _DISPLAY_FIELD_MAP = build_display_field_map(ALL_TOOL_SPECS)
        except Exception:  # noqa: BLE001 — never let registry import break composing
            logger.exception("presenter: failed to build display_field map from registry")
            _DISPLAY_FIELD_MAP = {}
    return _DISPLAY_FIELD_MAP


# ---------------------------------------------------------------------------
# valid-status allowlist — every declared Literal `status` per tool output model
# ---------------------------------------------------------------------------
# Sanitizes the OUTGOING action status the composer puts in the LLM payload:
# only a schema-declared status value is forwarded; anything else (id-like,
# oversized, malformed) is downgraded by the caller. VALUE-based, not shape-based
# — `member_id_755682022` is alnum/underscore yet NOT a declared status → rejected.


def build_valid_status_map(specs: Iterable[Any]) -> dict[str, frozenset[str]]:
    """Introspect ToolSpecs → ``tool_name -> frozenset(declared status literals)``."""
    mapping: dict[str, frozenset[str]] = {}
    for spec in specs:
        name = getattr(spec, "name", None)
        output_model = getattr(spec, "output_model", None)
        if not isinstance(name, str) or output_model is None:
            continue
        statuses: set[str] = set()
        for variant in _union_members(output_model):
            statuses.update(_status_literals(variant))
        if statuses:
            mapping[name] = frozenset(statuses)
    return mapping


_VALID_STATUS_MAP: dict[str, frozenset[str]] | None = None


def set_valid_status_map(mapping: dict[str, frozenset[str]]) -> None:
    """Install the tool → valid-status-set map (app startup or tests)."""
    global _VALID_STATUS_MAP
    _VALID_STATUS_MAP = dict(mapping)


def _valid_status_map() -> dict[str, frozenset[str]]:
    global _VALID_STATUS_MAP
    if _VALID_STATUS_MAP is None:
        try:
            from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS  # lazy

            _VALID_STATUS_MAP = build_valid_status_map(ALL_TOOL_SPECS)
        except Exception:  # noqa: BLE001 — never let registry import break composing
            logger.exception("presenter: failed to build valid-status map from registry")
            _VALID_STATUS_MAP = {}
    return _VALID_STATUS_MAP


def is_known_domain_status(tool_name: str, status: str) -> bool:
    """True iff ``status`` is a declared Literal status of ``tool_name``'s output
    model — a schema-defined enum value safe to forward to the LLM, never an
    internal id. Tools/statuses absent from the registry → False (caller
    downgrades to a safe sentinel)."""
    return status in _valid_status_map().get(tool_name, frozenset())


# ---------------------------------------------------------------------------
# override registry — tools whose narration is more than one literal field
# ---------------------------------------------------------------------------


# Overrides return the RAW value; the API boundary applies `_finalize` (cap + non-empty),
# so an override can't accidentally bypass the contract.
def _present_web_search(output: Mapping[str, Any], domain_status: str) -> Any:
    return output.get("raw_text")


def _present_recall_memory(output: Mapping[str, Any], domain_status: str) -> Any:
    hits = output.get("hits")
    if not isinstance(hits, list):
        hits = []
    return "\n".join(
        str(hit.get("content"))
        for hit in hits
        if isinstance(hit, Mapping) and hit.get("content") is not None
    )


_OVERRIDES: dict[str, Callable[[Mapping[str, Any], str], Any]] = {
    "web_search": _present_web_search,
    "recall_memory": _present_recall_memory,
}


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def render_display_text(tool_name: str, output: Any, *, domain_status: str) -> str:
    """Public-safe, non-empty, length-capped display string for an ``ok`` step.

    ``output`` is the executor's ``parsed_output`` (a dict). Resolution order:
    per-tool override → declared ``display_field`` (via the registry map) projection →
    deny-by-default (fixed phrase + metric). Projects ONLY the safe field; never emits
    internal ids / scores / metadata.
    """
    if not isinstance(domain_status, str):  # unhashable/non-str → safe deny path
        domain_status = "unknown"
    mapping = _as_mapping(output)

    override = _OVERRIDES.get(tool_name)
    if override is not None and mapping is not None:
        return _finalize(override(mapping, domain_status))  # boundary enforces cap+non-empty

    field = _display_field_map().get((tool_name, domain_status)) if mapping is not None else None
    if field is None:
        key = (tool_name, domain_status)
        PRESENTER_FALLBACK_COUNTS[key] = PRESENTER_FALLBACK_COUNTS.get(key, 0) + 1
        logger.warning(
            "presenter: no display_field/override for tool=%s status=%s — "
            "deny-by-default fallback",
            tool_name,
            domain_status,
        )
        return _DENY_BY_DEFAULT

    return _finalize(mapping.get(field))


def render_execution_failure(
    tool_name: str, exec_status: str, error_summary: str | None
) -> str:
    """Public-safe phrase for an executor-level failure status. ``error_summary`` is
    accepted for call-site logging but intentionally NOT echoed (no internal leak)."""
    if not isinstance(exec_status, str):  # unhashable/non-str → generic safe default
        exec_status = "error"
    return _EXECUTION_FAILURE_PHRASES.get(exec_status, _EXECUTION_FAILURE_DEFAULT)
