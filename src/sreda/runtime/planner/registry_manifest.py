"""Planner-safe registry manifest — Sub-A12 Phase E PR-2a #10b.

Builds ONE tenant-scoped, immutable ``PlannerManifest`` that is the single
source of ToolSpec truth for the three planner consumers:

- **prompt_builder**: ``render_tool_registry_block(manifest.specs)``
- **plan_compiler**: ``compile(plan, registry=manifest.by_name)``
- **validator / executor**: ``validate_plan(plan, registry=manifest.by_name)``

Design constraints (accepted plan §PR-2a.1):
- The legacy full registry (``MIGRATED_TOOL_SPECS`` / ``ALL_TOOL_SPECS`` in
  ``services.tool_schemas.specs``) is NEVER modified here.  This module reads
  from it as a library consumer.
- Per-tenant ``enabled_names`` selection and the durable-write ⇒ adapter
  fail-closed invariant are **out of scope** for this module.  They land in
  PR-2b (tenant config wiring) and issue #8b-3 (adapter enforcement) respectively.
- Pure: no I/O, no settings reads, no global mutation, fully deterministic.

#8b-3 adapter-invariant hook (future, do NOT implement here):
    ``manifest.durable_write_names`` is the set of tool names where
    ``spec.is_durable_write`` is True.  The #8b-3 fail-closed adapter check
    will consume this set to assert that every durable-write tool has a
    registered recovery adapter before accepting the manifest at startup.
    The field is populated here so #8b-3 can be a pure reader with no
    manifest changes.

PR-2b wiring note (future, do NOT implement here):
    ``build_planner_manifest`` should be called once per tenant at
    configuration load time, with ``all_specs=MIGRATED_TOOL_SPECS`` (or
    ``ALL_TOOL_SPECS``) and ``enabled_names`` sourced from the tenant's
    feature-flag / settings entry (e.g. ``tenant_config.planner_tools``).
    The returned ``PlannerManifest`` is stored on the tenant context and
    passed down to orchestrator → prompt_builder / plan_compiler / validator.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from sreda.services.tool_schemas.base import ToolSpec


@dataclass(frozen=True)
class PlannerManifest:
    """Immutable, validated subset of ToolSpecs for a single tenant/turn.

    Constructed only via :func:`build_planner_manifest` — do NOT
    instantiate directly; the builder enforces all fail-closed invariants.

    Attributes
    ----------
    specs:
        The enabled subset of ToolSpecs in **stable** order (preserving the
        position of each spec in the ``all_specs`` iterable passed to the
        builder).  Order is deterministic regardless of the ``enabled_names``
        collection type (set iteration order is NOT used).

    All derived helpers (``by_name``, ``names``, ``durable_write_names``) are
    computed once in ``__post_init__`` and stored in private slots; the
    public ``@property`` accessors expose them as read-only views.

    NOT pickle-serializable by design: ``_by_name`` holds a
    ``MappingProxyType`` (not picklable). The manifest is an in-memory,
    rebuild-from-source artifact — reconstruct it via
    :func:`build_planner_manifest`, do not pickle/deep-copy it across
    processes. (No current consumer serializes it; Codex A/B #10b R2 LOW.)
    """

    specs: tuple[ToolSpec, ...]

    # Private slots computed once in __post_init__.
    # ``field(init=False, repr=False, compare=False, hash=False)`` keeps the
    # dataclass frozen contract intact: the builder sets these via
    # object.__setattr__ inside __post_init__ before the instance is sealed.
    _by_name: Mapping[str, ToolSpec] = field(
        default_factory=dict, init=False, repr=False, compare=False, hash=False
    )
    _names: frozenset[str] = field(
        default=frozenset(), init=False, repr=False, compare=False, hash=False
    )
    _durable_write_names: frozenset[str] = field(
        default=frozenset(), init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        # Normalize specs to an immutable tuple FIRST (Codex A/B #10b R2, both
        # MAJOR): the ``specs: tuple[...]`` annotation is NOT runtime-enforced,
        # so a direct ``PlannerManifest(specs=[...])`` could pass a list that
        # the caller later mutates, diverging specs from the read-only
        # by_name / names / durable_write_names. Coercing here makes specs
        # immutable regardless of input. (tuple(tuple) is a cheap no-op.)
        object.__setattr__(self, "specs", tuple(self.specs))
        # Fail-closed EVEN ON DIRECT CONSTRUCTION (Codex A/B #10b R1, both MAJOR):
        # the class is public, so PlannerManifest(specs=...) must not be able to
        # produce an empty manifest or a duplicate-name by_name that silently
        # last-wins. The builder catches these earlier with richer messages;
        # this is the belt-and-braces invariant on the type itself.
        if not self.specs:
            raise ValueError(
                "PlannerManifest: specs must be non-empty — an empty manifest "
                "gives the planner no tools (configuration error)."
            )
        by_name: dict[str, ToolSpec] = {}
        for spec in self.specs:
            if spec.name in by_name:
                raise ValueError(
                    f"PlannerManifest: duplicate ToolSpec name {spec.name!r} in "
                    f"specs — cannot build an unambiguous by_name map."
                )
            by_name[spec.name] = spec
        names: frozenset[str] = frozenset(by_name)
        durable_write_names: frozenset[str] = frozenset(
            s.name for s in self.specs if s.is_durable_write
        )
        # Store by_name as a read-only MappingProxyType so a runtime caller
        # cannot mutate it (add/clear) and diverge it from specs/names/
        # durable_write_names (Codex A/B #10b R1, both MAJOR). bypass frozen
        # guard via object.__setattr__ (standard frozen-dataclass pattern).
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))
        object.__setattr__(self, "_names", names)
        object.__setattr__(self, "_durable_write_names", durable_write_names)

    # ------------------------------------------------------------------
    # Public read-only views
    # ------------------------------------------------------------------

    @property
    def by_name(self) -> Mapping[str, ToolSpec]:
        """Name → ToolSpec map; SAME object references as in ``specs``.

        Satisfies the ``Mapping[str, ToolSpec]`` type accepted by
        ``plan_compiler.compile(plan, registry=...)`` and
        ``validator.validate_plan(plan, registry=...)``.
        """
        return self._by_name

    @property
    def names(self) -> frozenset[str]:
        """Frozenset of enabled tool names — cheap membership test."""
        return self._names

    @property
    def durable_write_names(self) -> frozenset[str]:
        """Names of durable-write tools (``spec.is_durable_write`` is True).

        Informational for callers.  The #8b-3 fail-closed adapter invariant
        will consume this to assert that every name here has a registered
        recovery adapter — do NOT enforce adapter presence in this module,
        that is #8b-3's job.

        A durable write satisfies: ``effect='write'``,
        ``side_effect_class='transactional_write'``, and
        ``request_local=False``.  Request-local writes (e.g.
        ``reply_with_buttons``) and read tools are excluded.
        """
        return self._durable_write_names


def build_planner_manifest(
    *,
    all_specs: Iterable[ToolSpec],
    enabled_names: Collection[str],
) -> PlannerManifest:
    """Build a :class:`PlannerManifest` from the full registry and an enabled-name set.

    Fail-closed — raises ``ValueError`` on any of the following:

    1. ``enabled_names`` is empty (an empty manifest gives the planner no
       tools; that is a configuration error, not a silent no-op).
    2. Any name in ``enabled_names`` is not present in ``all_specs``
       (guards against typos silently shrinking the manifest).
    3. Among the specs whose name is in ``enabled_names``, two share the
       same ``.name`` (defensive: ambiguous — cannot build a correct
       ``by_name`` map).

    Parameters
    ----------
    all_specs:
        Full source registry (e.g. ``MIGRATED_TOOL_SPECS`` or
        ``ALL_TOOL_SPECS`` from ``sreda.services.tool_schemas.specs``).
        Consumed once; order determines the stable order of ``specs`` in
        the returned manifest.
    enabled_names:
        The tenant-scoped subset of tool names to include.  May be any
        ``Collection[str]`` (list, set, frozenset …).  Iteration order is
        NOT used for manifest order — ``all_specs`` order is canonical.

    Returns
    -------
    PlannerManifest
        Immutable, validated manifest containing exactly the requested
        subset in ``all_specs`` order.
    """
    if not enabled_names:
        raise ValueError(
            "build_planner_manifest: enabled_names must be non-empty. "
            "An empty manifest gives the planner no tools — this is a "
            "configuration error, not a valid no-op state."
        )

    enabled_set: frozenset[str] = frozenset(enabled_names)

    # Single pass over all_specs: collect matching specs in source order.
    # Track the SOURCE index in all_specs (not the selected-list index) so
    # duplicate diagnostics point at the real registry positions even when
    # disabled specs sit between the two collisions (Codex A/B #10b R1 MINOR).
    selected: list[ToolSpec] = []
    seen_names: dict[str, int] = {}  # name → first occurrence index in all_specs

    for src_idx, spec in enumerate(all_specs):
        if spec.name not in enabled_set:
            continue
        if spec.name in seen_names:
            raise ValueError(
                f"build_planner_manifest: duplicate ToolSpec name "
                f"{spec.name!r} in all_specs (first at index "
                f"{seen_names[spec.name]}, second at index {src_idx}). "
                f"Cannot build an unambiguous by_name map — fix the registry "
                f"so every name is unique."
            )
        seen_names[spec.name] = src_idx
        selected.append(spec)

    # Guard: every requested name must have been found.
    found_names: frozenset[str] = frozenset(seen_names)
    missing: frozenset[str] = enabled_set - found_names
    if missing:
        raise ValueError(
            f"build_planner_manifest: enabled_names contains "
            f"{sorted(missing)!r} which are not present in all_specs. "
            f"Fix the enabled-name list (typo?) or add the missing specs."
        )

    return PlannerManifest(specs=tuple(selected))


__all__ = [
    "PlannerManifest",
    "build_planner_manifest",
]
