"""LLMPromptRegistry — composer LLM-path prompts keyed by ``llm_prompt_key``.

Sub-A12 Phase D.2 — the LLM-path twin of ``ComposerRegistry`` (which
holds Jinja templates for ``kind='template'``). This holds static
system-prompt specs for ``kind='llm'``.

Why a registry (same rationale as ComposerRegistry, Group 6.5):

- the planner system-prompt allowlist is generated from
  ``LLM_PROMPT_REGISTRY.prompt_keys()`` — the planner can only emit a
  ``llm_prompt_key`` that exists here
- the validator (Phase B) rejects plans whose ``llm_prompt_key`` is
  not registered
- ``snapshot_hash()`` is recorded in ``planner_executions`` so a
  registry change between plan-time and compose-time is detectable
  (same deploy-race guard the template registry has)

No silent fallback, no inline prompt strings, no ``if key == 'x'``
routing anywhere else. Adding a prompt = one entry in
``llm_prompts_housewife.py``.
"""

from __future__ import annotations

import hashlib

from sreda.services.composer.llm_prompts_housewife import (
    HOUSEWIFE_LLM_PROMPTS,
    LLMPromptSpec,
)


class UnknownLLMPromptError(KeyError):
    """``llm_prompt_key`` not in the registry — the Phase B validator
    should have rejected this; if it reaches the composer it's a
    deploy race (prompt removed between validation and compose) and the
    composer falls back to the generic error reply."""


class ComposerInputError(ValueError):
    """A prompt's ``required_keys`` are missing/blank in the resolved
    ``template_data``. Raised by the registry's ``validate_data`` so
    ``llm_composer`` fails fast BEFORE the LLM call — sending a
    half-empty prompt would invite the model to fill the gaps
    (i.e. fabricate), the exact failure the architecture kills."""


class LLMPromptRegistry:
    """Flat dict of ``LLMPromptSpec`` keyed by ``llm_prompt_key``.

    Single source of truth — every key the planner may emit must be
    registered. Unknown keys raise ``UnknownLLMPromptError``.
    """

    def __init__(self) -> None:
        self._specs: dict[str, LLMPromptSpec] = {}

    def register(self, llm_prompt_key: str, spec: LLMPromptSpec) -> None:
        """Store a prompt spec. Re-registering the same key overwrites —
        used by tests and registry-build helpers."""
        if not llm_prompt_key:
            raise ValueError("llm_prompt_key must be non-empty")
        if not spec.system_prompt:
            raise ValueError(
                f"LLMPromptSpec for {llm_prompt_key!r} has empty system_prompt"
            )
        self._specs[llm_prompt_key] = spec

    def get(self, llm_prompt_key: str) -> LLMPromptSpec:
        """Return the spec for ``llm_prompt_key``.

        Raises ``UnknownLLMPromptError`` if not registered."""
        spec = self._specs.get(llm_prompt_key)
        if spec is None:
            raise UnknownLLMPromptError(
                f"llm_prompt_key={llm_prompt_key!r} not registered. "
                f"Known: {sorted(self._specs.keys())}"
            )
        return spec

    def validate_data(
        self, llm_prompt_key: str, data: dict[str, object],
    ) -> None:
        """Check that every ``required_keys`` entry for this prompt is
        present and non-blank in ``data``.

        Raises ``UnknownLLMPromptError`` if the key isn't registered,
        ``ComposerInputError`` if a required field is missing or blank.

        "Blank" = None, empty string, empty list/dict. A required field
        present but empty is as useless to the narrator as an absent
        one — both invite gap-filling.
        """
        spec = self.get(llm_prompt_key)
        missing: list[str] = []
        for required in sorted(spec.required_keys):
            value = data.get(required)
            # Codex D.2 R1 MINOR A#5 — whitespace-only strings are as
            # useless as empty ones; treat "   " as blank. 0 / False
            # stay valid (e.g. added_count=0).
            if isinstance(value, str):
                is_blank = not value.strip()
            else:
                is_blank = value is None or value == [] or value == {}
            if is_blank:
                missing.append(required)
        if missing:
            raise ComposerInputError(
                f"llm_prompt_key={llm_prompt_key!r} missing/blank required "
                f"data keys: {missing}. Present keys: {sorted(data.keys())}"
            )

    def prompt_keys(self) -> list[str]:
        """Sorted registered keys — builds the planner allowlist block +
        the validator allowlist."""
        return sorted(self._specs.keys())

    def descriptions(self) -> dict[str, str]:
        """``key → description`` for the planner allowlist block, so the
        planner knows when to pick each key."""
        return {k: self._specs[k].description for k in self.prompt_keys()}

    def snapshot_hash(self) -> str:
        """Stable hash over the registry contents.

        Recorded in ``planner_executions`` at Phase B so the compose
        stage can detect a deploy between validation and render. Hash
        includes key + system_prompt + sorted required_keys so any
        material change (new key, edited prompt body, changed required
        fields) is caught.
        """
        h = hashlib.sha256()
        for key in self.prompt_keys():
            spec = self._specs[key]
            h.update(key.encode("utf-8"))
            h.update(b"\x00")
            h.update(spec.system_prompt.encode("utf-8"))
            h.update(b"\x00")
            for rk in sorted(spec.required_keys):
                h.update(rk.encode("utf-8"))
                h.update(b"\x01")
            h.update(b"\x00")
        return h.hexdigest()


def _build_default_registry() -> LLMPromptRegistry:
    reg = LLMPromptRegistry()
    for key, spec in HOUSEWIFE_LLM_PROMPTS.items():
        reg.register(key, spec)
    return reg


LLM_PROMPT_REGISTRY = _build_default_registry()
"""Module-level default registry. Most callers use ``LLM_PROMPT_REGISTRY``
directly; tests can build their own ``LLMPromptRegistry`` to inject
custom prompts."""


__all__ = [
    "ComposerInputError",
    "LLM_PROMPT_REGISTRY",
    "LLMPromptRegistry",
    "UnknownLLMPromptError",
]
