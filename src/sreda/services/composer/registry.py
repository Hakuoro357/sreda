"""ComposerRegistry — Jinja2 templates keyed by ``template_id``.

Sub-A5 foundation (Group 6.5 of architecture plan). Templates live in
``services/composer/templates_housewife.py`` as a flat dict so:

- adding a template = one Python edit (no Jinja files on disk)
- planner system-prompt enum is generated from ``REGISTRY.template_ids()``
- registry snapshot hash is recorded in ``planner_executions`` for
  race protection (template removed between Phase B validation and
  Phase D compose)
- CI test ``tests/unit/test_composer_registry.py`` asserts every
  template renders with its sample data — drift caught at commit time

Top-5 templates included now (matching Sub-A4 tool coverage). The
``partial_with_compose_error`` fallback (Group 6.5 race path) is also
present so the executor's "tools committed, compose failed" branch has
a real target — not a hypothetical id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from jinja2 import (
    Environment,
    StrictUndefined,
    Template,
    TemplateError,
)

from sreda.services.clarification_contract import clarification_field_ru
from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES


class UnknownTemplateError(KeyError):
    """``template_id`` not in the registry — validator should have caught
    this in Phase B; if it slips through to compose, it's a race
    condition (Group 6.5 ``compose_failure_after_execution`` path)."""


@dataclass(frozen=True)
class TemplateEntry:
    """One row in the registry — Jinja source + cached compiled template."""

    template_id: str
    source: str
    template: Template


class ComposerRegistry:
    """Flat dict of compiled Jinja2 templates keyed by ``template_id``.

    Single source of truth — every template_id the planner can emit
    must be registered here. Unknown ids raise ``UnknownTemplateError``.
    """

    def __init__(self) -> None:
        # autoescape=False — output goes to Telegram as plain text
        # (we don't use HTML send_message mode). With autoescape ON,
        # variables containing ``&`` / ``<`` / ``>`` would render as
        # HTML entities (``M&amp;M``), which is wrong for plain-text
        # delivery. Codex review 2026-05-26 MEDIUM fix.
        # If we ever add HTML admin pages that reuse templates,
        # they should use their own Environment instance with
        # ``autoescape=True``.
        # StrictUndefined surfaces template-data typos as exceptions
        # rather than silently rendering empty — aligns with the
        # "no silent fallback" architectural principle.
        self._env = Environment(
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=False,
        )
        # Register the ``clarify_ru`` filter so clarification templates can
        # render ``{{ field | clarify_ru }}`` instead of an inline if/elif chain.
        # Imported from the zero-intra-project-import leaf module — cycle-safe.
        self._env.filters["clarify_ru"] = clarification_field_ru
        self._entries: dict[str, TemplateEntry] = {}

    def register(self, template_id: str, source: str) -> None:
        """Compile and store a template. Re-registering the same id
        overwrites — used by tests and by registry-build helpers."""
        if not template_id:
            raise ValueError("template_id must be non-empty")
        compiled = self._env.from_string(source)
        self._entries[template_id] = TemplateEntry(
            template_id=template_id,
            source=source,
            template=compiled,
        )

    def render(self, template_id: str, data: dict[str, Any]) -> str:
        """Render the template with ``data`` and return the result.

        Raises ``UnknownTemplateError`` if ``template_id`` isn't registered.
        Jinja's ``StrictUndefined`` propagates missing-variable errors so
        a planner that forgot to pass a required field fails loud.
        """
        entry = self._entries.get(template_id)
        if entry is None:
            raise UnknownTemplateError(
                f"template_id={template_id!r} not registered. "
                f"Known: {sorted(self._entries.keys())}"
            )
        try:
            return entry.template.render(**data)
        except TemplateError as exc:
            # Re-raise with template_id context so the caller's
            # compose_failure_after_execution path can log it.
            raise TemplateError(
                f"render of template_id={template_id!r} failed: {exc}"
            ) from exc

    def template_ids(self) -> list[str]:
        """Sorted list of registered ``template_id`` — used to build the
        planner system-prompt enum + the validator allowlist."""
        return sorted(self._entries.keys())

    def snapshot_hash(self) -> str:
        """Stable hash of the current registry contents.

        Recorded in ``planner_executions.composer_registry_snapshot_hash``
        at Phase B (validation) so the compose stage can detect a deploy
        between validation and render (Group 6.5 race guard).

        Hash includes both id and source so a same-id-different-body
        template change is caught.
        """
        h = hashlib.sha256()
        for template_id in sorted(self._entries.keys()):
            entry = self._entries[template_id]
            h.update(template_id.encode("utf-8"))
            h.update(b"\x00")
            h.update(entry.source.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()


def _build_default_registry() -> ComposerRegistry:
    reg = ComposerRegistry()
    for template_id, source in HOUSEWIFE_TEMPLATES.items():
        reg.register(template_id, source)
    return reg


REGISTRY = _build_default_registry()
"""Module-level default registry. Most callers use ``render(...)`` and
``REGISTRY.template_ids()`` directly; tests can spin up their own
``ComposerRegistry`` instance to inject custom templates."""


def render(template_id: str, data: dict[str, Any]) -> str:
    """Convenience shortcut for ``REGISTRY.render(...)``."""
    return REGISTRY.render(template_id, data)


__all__ = [
    "ComposerRegistry",
    "REGISTRY",
    "TemplateEntry",
    "UnknownTemplateError",
    "render",
]
