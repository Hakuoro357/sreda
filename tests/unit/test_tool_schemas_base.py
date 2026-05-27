"""Tests for ``services/tool_schemas/base.py`` — ToolSpec + ToolOutput.

The registry entry contract:
- ``effect='write'`` requires non-empty ``write_domains`` (Group 2 safety
  invariant — scheduler relies on domain overlap to detect conflicts).
- ``side_effect_class='external_side_effect'`` is forbidden in MVP
  (Group 6.3 — until explicit compensation/abort design lands, no tool
  may declare itself as making non-rollback-able external calls).
- ``side_effect_class='read_only'`` must be paired with ``effect='read'``
  (consistency invariant).
- ``timeout_seconds`` is bounded [1, 600] — anything outside is a typo
  or misunderstanding of the per-tool budget contract.

``ToolOutputContractViolation`` is the discriminated branch the wrapper
returns when a tool's raw output doesn't match any registered union
variant. Executor halts the plan and logs to ``planner_gaps`` on this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Union

import pytest
from pydantic import BaseModel, ValidationError

from sreda.services.tool_schemas.base import (
    ToolOutput,
    ToolOutputContractViolation,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# ToolOutput base + ContractViolation
# ---------------------------------------------------------------------------


def test_tool_output_base_requires_status() -> None:
    with pytest.raises(ValidationError):
        ToolOutput()  # type: ignore[call-arg]


def test_tool_output_contract_violation_constructs() -> None:
    cv = ToolOutputContractViolation(
        raw_output="ok:weird_format:42",
        tool_name="add_shopping_items",
        timestamp=datetime.now(timezone.utc),
    )
    assert cv.status == "contract_violation"
    assert cv.tool_name == "add_shopping_items"


def test_tool_output_contract_violation_status_is_literal() -> None:
    # Cannot override the discriminator status — must remain literal value
    with pytest.raises(ValidationError):
        ToolOutputContractViolation(
            status="something_else",  # type: ignore[arg-type]
            raw_output="x",
            tool_name="y",
            timestamp=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# ToolSpec — basic construction
# ---------------------------------------------------------------------------


class _InOk(BaseModel):
    text: str


class _OutOk(ToolOutput):
    status: Literal["ok"] = "ok"


def _minimal_read_spec(**overrides: object) -> ToolSpec:
    base = dict(
        name="list_shopping",
        description="Show shopping list",
        family="shopping",
        effect="read",
        read_domains=["shopping"],
        write_domains=[],
        input_model=_InOk,
        output_model=_OutOk,
    )
    base.update(overrides)
    return ToolSpec(**base)  # type: ignore[arg-type]


def _minimal_write_spec(**overrides: object) -> ToolSpec:
    base = dict(
        name="add_shopping_items",
        description="Add items to shopping",
        family="shopping",
        effect="write",
        read_domains=[],
        write_domains=["shopping"],
        input_model=_InOk,
        output_model=_OutOk,
    )
    base.update(overrides)
    return ToolSpec(**base)  # type: ignore[arg-type]


def test_tool_spec_minimal_read_ok() -> None:
    spec = _minimal_read_spec()
    assert spec.name == "list_shopping"
    assert spec.effect == "read"
    assert spec.timeout_seconds == 15  # default
    assert spec.side_effect_class == "transactional_write"  # default
    assert spec.parallel_safe is False  # default


def test_tool_spec_minimal_write_ok() -> None:
    spec = _minimal_write_spec()
    assert spec.effect == "write"
    assert spec.write_domains == ["shopping"]


# ---------------------------------------------------------------------------
# ToolSpec — invariants
# ---------------------------------------------------------------------------


def test_tool_spec_write_without_write_domains_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _minimal_write_spec(write_domains=[])
    assert "write_domains" in str(exc.value).lower()


def test_tool_spec_external_side_effect_forbidden_in_mvp() -> None:
    with pytest.raises(ValidationError) as exc:
        _minimal_write_spec(side_effect_class="external_side_effect")
    msg = str(exc.value).lower()
    assert "external" in msg or "forbidden" in msg or "mvp" in msg


def test_tool_spec_read_only_requires_effect_read() -> None:
    # read_only side_effect_class makes no sense for effect=write
    with pytest.raises(ValidationError):
        _minimal_write_spec(side_effect_class="read_only")


def test_tool_spec_read_only_with_effect_read_ok() -> None:
    spec = _minimal_read_spec(side_effect_class="read_only")
    assert spec.side_effect_class == "read_only"


# ---------------------------------------------------------------------------
# ToolSpec — timeout bounds
# ---------------------------------------------------------------------------


def test_tool_spec_timeout_below_minimum_rejected() -> None:
    with pytest.raises(ValidationError):
        _minimal_read_spec(timeout_seconds=0)


def test_tool_spec_timeout_above_maximum_rejected() -> None:
    # 600 is the absolute cap (10 min — well above any reasonable tool)
    with pytest.raises(ValidationError):
        _minimal_read_spec(timeout_seconds=601)


def test_tool_spec_timeout_in_range_ok() -> None:
    for t in (1, 5, 15, 90, 300, 600):
        spec = _minimal_read_spec(timeout_seconds=t)
        assert spec.timeout_seconds == t


# ---------------------------------------------------------------------------
# ToolSpec — strict mode (extra fields forbidden)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex Sub-A-77 item #4 R6 MAJOR #1 — @field_validator guard at registry
# ---------------------------------------------------------------------------


def test_tool_spec_rejects_input_model_with_field_validator() -> None:
    """A planner-facing ``input_model`` declaring ``@field_validator``
    is rejected at ToolSpec construction time unless
    ``allow_field_validators=True`` is explicitly set. The plan
    validator's refs-present path cannot enforce those decorators
    (TypeAdapter doesn't run them) — silent gap; forced explicit
    acknowledgement is safer."""
    from pydantic import field_validator

    class _WithFieldValidator(BaseModel):
        title: str

        @field_validator("title")
        @classmethod
        def _check_title(cls, v: str) -> str:
            if not v:
                raise ValueError("empty title")
            return v

    with pytest.raises(ValidationError) as exc:
        _minimal_read_spec(input_model=_WithFieldValidator)
    msg = str(exc.value).lower()
    assert "field_validator" in msg or "allow_field_validators" in msg


def test_tool_spec_accepts_field_validator_when_explicitly_allowed() -> None:
    """The escape hatch ``allow_field_validators=True`` permits the
    same input_model, forcing the operator to acknowledge the gap."""
    from pydantic import field_validator

    class _WithFieldValidator(BaseModel):
        title: str

        @field_validator("title")
        @classmethod
        def _check_title(cls, v: str) -> str:
            return v

    spec = _minimal_read_spec(
        input_model=_WithFieldValidator,
        allow_field_validators=True,
    )
    assert spec.allow_field_validators is True


def test_tool_spec_default_allow_field_validators_is_false() -> None:
    spec = _minimal_read_spec()
    assert spec.allow_field_validators is False


# Codex R7 close-out: the @field_validator guard must walk NESTED
# BaseModel input fields too. ``_validate_nested_basemodel_partial`` in
# validator.py exercises the same TypeAdapter-bypass path on nested
# models — a nested @field_validator would silently miss enforcement.

def test_tool_spec_rejects_nested_basemodel_with_field_validator() -> None:
    from pydantic import field_validator

    class _Author(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _check_name(cls, v: str) -> str:
            return v

    class _Post(BaseModel):
        title: str
        author: _Author

    with pytest.raises(ValidationError) as exc:
        _minimal_read_spec(input_model=_Post)
    msg = str(exc.value).lower()
    # The path should mention the nested path (author.name).
    assert "author" in msg


def test_tool_spec_rejects_list_of_nested_with_field_validator() -> None:
    from pydantic import field_validator

    class _Tag(BaseModel):
        slug: str

        @field_validator("slug")
        @classmethod
        def _check_slug(cls, v: str) -> str:
            return v

    class _Post(BaseModel):
        title: str
        tags: list[_Tag]

    with pytest.raises(ValidationError) as exc:
        _minimal_read_spec(input_model=_Post)
    msg = str(exc.value).lower()
    # Validator should walk list[Tag] and find the nested validator.
    assert "tags" in msg or "slug" in msg


def test_tool_spec_rejects_optional_nested_with_field_validator() -> None:
    from pydantic import field_validator

    class _Author(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _check_name(cls, v: str) -> str:
            return v

    class _Post(BaseModel):
        title: str
        author: _Author | None = None

    with pytest.raises(ValidationError) as exc:
        _minimal_read_spec(input_model=_Post)
    msg = str(exc.value).lower()
    assert "author" in msg


def test_tool_spec_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(  # type: ignore[call-arg]
            name="x",
            description="x",
            family="shopping",
            effect="read",
            read_domains=["shopping"],
            write_domains=[],
            input_model=_InOk,
            output_model=_OutOk,
            mystery_meta="should not be accepted",
        )


def test_tool_spec_name_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        _minimal_read_spec(name="")


def test_tool_spec_description_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        _minimal_read_spec(description="")


# ---------------------------------------------------------------------------
# Discriminator union pattern (sanity check that base works inside one)
# ---------------------------------------------------------------------------


class _OutA(ToolOutput):
    status: Literal["added"] = "added"
    added_count: int


class _OutB(ToolOutput):
    status: Literal["partial_duplicate"] = "partial_duplicate"
    new: list[str]
    duplicates: list[str]


def test_discriminator_union_routes_by_status_added() -> None:
    from pydantic import Field, TypeAdapter
    from typing import Annotated

    Union_ = Annotated[Union[_OutA, _OutB], Field(discriminator="status")]
    adapter = TypeAdapter(Union_)

    parsed = adapter.validate_python({"status": "added", "added_count": 3})
    assert isinstance(parsed, _OutA)
    assert parsed.added_count == 3


def test_discriminator_union_routes_by_status_partial() -> None:
    from pydantic import Field, TypeAdapter
    from typing import Annotated

    Union_ = Annotated[Union[_OutA, _OutB], Field(discriminator="status")]
    adapter = TypeAdapter(Union_)

    parsed = adapter.validate_python(
        {"status": "partial_duplicate", "new": ["a"], "duplicates": ["b"]}
    )
    assert isinstance(parsed, _OutB)
    assert parsed.new == ["a"]


def test_discriminator_union_unknown_status_rejected() -> None:
    # The wrapper layer is what turns this into a ToolOutputContractViolation
    # at runtime; the union itself just rejects unknown discriminator values.
    from pydantic import Field, TypeAdapter
    from typing import Annotated

    Union_ = Annotated[Union[_OutA, _OutB], Field(discriminator="status")]
    adapter = TypeAdapter(Union_)

    with pytest.raises(ValidationError):
        adapter.validate_python({"status": "weird_unknown", "anything": True})
