"""Unit tests for the planner composer presenters (#110, Phase 2a).

Production passes ``StepResult.parsed_output`` — a plain ``dict`` — to
``render_display_text`` (the model was already ``model_dump``-ed in the executor).
So these tests drive the PRODUCTION shape: dict inputs + a ``(tool, status) -> field``
map built by introspecting sample ToolSpec-like stand-ins.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated, ClassVar, Literal, Union

import pytest
from pydantic import BaseModel, Field

from sreda.services.composer import presenters
from sreda.services.composer.presenters import (
    DISPLAY_TEXT_MAX_CHARS,
    PRESENTER_FALLBACK_COUNTS,
    build_display_field_map,
    render_display_text,
    render_execution_failure,
    set_display_field_map,
)


# --- sample output-model variants (declare __display_field__ like real tools will) ---


class AnnotatedTextOutput(BaseModel):
    __display_field__: ClassVar[str | None] = "summary"
    status: Literal["ok"] = "ok"
    summary: str = ""
    internal_id: str = ""
    score: float = 0.0
    metadata: dict[str, str] = {}


class AnnotatedListOutput(BaseModel):
    __display_field__: ClassVar[str | None] = "items"
    status: Literal["ok"] = "ok"
    items: list[object] = []


class AnnotatedDictOutput(BaseModel):
    __display_field__: ClassVar[str | None] = "payload"
    status: Literal["ok"] = "ok"
    payload: dict[str, object] = {}


class AnnotatedNoneOutput(BaseModel):
    __display_field__: ClassVar[str | None] = "maybe_text"
    status: Literal["empty"] = "empty"
    maybe_text: str | None = None


class UnannotatedOutput(BaseModel):
    status: Literal["ok"] = "ok"
    raw_text: str = ""
    internal_id: str = ""


_SAMPLE_SPECS = [
    SimpleNamespace(name="text_tool", output_model=AnnotatedTextOutput),
    SimpleNamespace(name="list_tool", output_model=AnnotatedListOutput),
    SimpleNamespace(name="dict_tool", output_model=AnnotatedDictOutput),
    SimpleNamespace(name="none_tool", output_model=AnnotatedNoneOutput),
    SimpleNamespace(name="unannotated_tool", output_model=UnannotatedOutput),
]


@pytest.fixture(autouse=True)
def _install_sample_map() -> None:
    PRESENTER_FALLBACK_COUNTS.clear()
    set_display_field_map(build_display_field_map(_SAMPLE_SPECS))
    yield
    presenters._DISPLAY_FIELD_MAP = None  # reset so other modules lazy-build from registry


# --- the map builder (introspection) ---


def test_build_display_field_map_reads_annotations_and_status_literals() -> None:
    m = build_display_field_map(_SAMPLE_SPECS)
    assert m[("text_tool", "ok")] == "summary"
    assert m[("list_tool", "ok")] == "items"
    assert m[("dict_tool", "ok")] == "payload"
    assert m[("none_tool", "empty")] == "maybe_text"
    # an un-annotated variant contributes nothing → deny-by-default by omission
    assert ("unannotated_tool", "ok") not in m


# --- render_display_text on PRODUCTION shape (dict) ---


def test_annotated_dict_projects_only_display_field() -> None:
    rendered = render_display_text(
        "text_tool",
        {
            "status": "ok",
            "summary": "Публичный текст",
            "internal_id": "secret-id",
            "score": 0.99,
            "metadata": {"source": "private"},
        },
        domain_status="ok",
    )
    assert rendered == "Публичный текст"
    assert "secret-id" not in rendered
    assert "0.99" not in rendered
    assert "private" not in rendered


def test_web_search_override_uses_raw_text() -> None:
    rendered = render_display_text(
        "web_search",
        {"status": "results", "raw_text": "Нашла результат", "url": "https://secret"},
        domain_status="results",
    )
    assert rendered == "Нашла результат"
    assert "secret" not in rendered


def test_recall_memory_override_joins_hit_content_without_metadata() -> None:
    rendered = render_display_text(
        "recall_memory",
        {
            "status": "recalled",
            "hits": [
                {
                    "content": "Первый факт",
                    "score": 0.91,
                    "source": "private-source",
                    "id": "mem_1",
                    "metadata": {"path": "private"},
                },
                {"content": "Второй факт", "score": 0.5, "id": "mem_2"},
            ],
        },
        domain_status="recalled",
    )
    assert rendered == "Первый факт\nВторой факт"
    assert "0.91" not in rendered
    assert "private-source" not in rendered
    assert "mem_1" not in rendered
    assert "metadata" not in rendered


def test_deny_by_default_never_guesses_fields_and_increments_metric() -> None:
    rendered = render_display_text(
        "unannotated_tool",
        {"status": "ok", "raw_text": "Не показывать", "internal_id": "secret-id"},
        domain_status="ok",
    )
    assert rendered == "Не могу безопасно показать результат."
    assert "Не показывать" not in rendered
    assert "secret-id" not in rendered
    assert PRESENTER_FALLBACK_COUNTS[("unannotated_tool", "ok")] == 1


def test_unmapped_tool_denies_and_increments_metric() -> None:
    rendered = render_display_text(
        "tool_not_in_registry", {"status": "ok", "x": 1}, domain_status="ok"
    )
    assert rendered == "Не могу безопасно показать результат."
    assert PRESENTER_FALLBACK_COUNTS[("tool_not_in_registry", "ok")] == 1


def test_coerces_list_dict_and_none_display_values() -> None:
    assert (
        render_display_text(
            "list_tool", {"status": "ok", "items": ["молоко", 5, None, "хлеб"]}, domain_status="ok"
        )
        == "молоко\n5\nхлеб"
    )
    assert (
        render_display_text(
            "dict_tool", {"status": "ok", "payload": {"b": 2, "a": "один"}}, domain_status="ok"
        )
        == '{"a":"один","b":2}'
    )
    assert (
        render_display_text(
            "none_tool", {"status": "empty", "maybe_text": None}, domain_status="empty"
        )
        == "Нет данных для показа."
    )


def test_caps_and_truncates_deterministically() -> None:
    rendered = render_display_text(
        "text_tool",
        {"status": "ok", "summary": "я" * (DISPLAY_TEXT_MAX_CHARS + 100)},
        domain_status="ok",
    )
    assert len(rendered) == DISPLAY_TEXT_MAX_CHARS
    assert rendered.endswith("...")


def test_render_display_text_also_accepts_a_model_instance() -> None:
    # belt-and-braces: if a caller passes a BaseModel, model_dump path still works
    rendered = render_display_text(
        "text_tool",
        AnnotatedTextOutput(summary="Из модели", internal_id="x"),
        domain_status="ok",
    )
    assert rendered == "Из модели"


class _UnionOk(BaseModel):
    __display_field__: ClassVar[str | None] = "text"
    status: Literal["results"] = "results"
    text: str = ""


class _UnionErr(BaseModel):
    __display_field__: ClassVar[str | None] = "message"
    status: Literal["error"] = "error"
    message: str = ""


_UnionOutput = Annotated[Union[_UnionOk, _UnionErr], Field(discriminator="status")]


def test_build_map_handles_real_annotated_discriminated_union() -> None:
    # production output_models are Annotated[Union[...], Field(discriminator="status")]
    m = build_display_field_map(
        [SimpleNamespace(name="union_tool", output_model=_UnionOutput)]
    )
    assert m[("union_tool", "results")] == "text"
    assert m[("union_tool", "error")] == "message"


def test_non_string_domain_status_denies_without_crashing() -> None:
    rendered = render_display_text(
        "text_tool", {"status": "ok", "summary": "x"}, domain_status=[]  # type: ignore[arg-type]
    )
    assert rendered == "Не могу безопасно показать результат."
    assert PRESENTER_FALLBACK_COUNTS[("text_tool", "unknown")] == 1


def test_render_execution_failure_non_string_status_is_safe() -> None:
    rendered = render_execution_failure("tool", None, "secret")  # type: ignore[arg-type]
    assert rendered == "Не получилось выполнить действие. Попробуй ещё раз позже."
    assert "secret" not in rendered


# --- Kimi review fixes (#2 boundary finalize, #5 empty field, #6 hits, #7 list-of-dicts) ---


class _EmptyFieldOutput(BaseModel):
    __display_field__: ClassVar[str | None] = ""  # mis-annotation
    status: Literal["ok"] = "ok"
    x: str = ""


def test_empty_string_display_field_annotation_is_ignored() -> None:
    m = build_display_field_map(
        [SimpleNamespace(name="ef_tool", output_model=_EmptyFieldOutput)]
    )
    assert ("ef_tool", "ok") not in m  # → deny-by-default, not silent empty-display


def test_override_empty_result_finalized_to_nonempty_at_boundary() -> None:
    # web_search override returns raw ""; the API boundary must finalize → non-empty
    assert (
        render_display_text(
            "web_search", {"status": "results", "raw_text": ""}, domain_status="results"
        )
        == "Нет данных для показа."
    )


def test_recall_memory_empty_or_nonlist_hits_is_safe() -> None:
    assert (
        render_display_text(
            "recall_memory", {"status": "recalled", "hits": []}, domain_status="recalled"
        )
        == "Нет данных для показа."
    )
    # non-list hits must NOT crash (e.g. malformed future schema)
    assert (
        render_display_text(
            "recall_memory", {"status": "recalled", "hits": 5}, domain_status="recalled"
        )
        == "Нет данных для показа."
    )


def test_list_of_dicts_coerced_as_json_not_python_repr() -> None:
    rendered = render_display_text(
        "list_tool", {"status": "ok", "items": [{"a": 1}, {"b": 2}]}, domain_status="ok"
    )
    assert rendered == '{"a":1}\n{"b":2}'  # recursive JSON coercion, not str(dict) repr


@pytest.mark.parametrize(
    ("exec_status", "expected"),
    [
        ("error", "Не получилось выполнить действие. Попробуй ещё раз позже."),
        ("timeout", "Действие заняло слишком много времени. Попробуй ещё раз."),
        ("plan_gap", "Не получилось корректно подготовить действие. Попробуй переформулировать."),
        ("arg_violation", "Не получилось выполнить действие из-за неверных параметров."),
        ("unknown_outcome", "Действие выполнилось, но результат не удалось безопасно распознать."),
        ("future_status", "Не получилось выполнить действие. Попробуй ещё раз позже."),
    ],
)
def test_render_execution_failure_is_public_safe(exec_status: str, expected: str) -> None:
    rendered = render_execution_failure(
        "tool", exec_status, "Traceback: secret token raw internal error"
    )
    assert rendered == expected
    assert "Traceback" not in rendered
    assert "secret" not in rendered
