# -*- coding: utf-8 -*-
"""Golden-тест карты возможностей (scripts/dev/gen_capabilities.py, lean v1).

Карта — внутренний тулинг (не часть продукта). Тест защищает:
  1. Контрольный кейс #143: операции над задачей = id_only, а finder find_by_title
     ЕСТЬ в object-finders (карта показывает «дыру», которую прозевали и я, и Codex).
  2. Курированные lookup_mode-метки не уехали (golden-значения плана).
  3. Все ключи LOOKUP_MODES — реальные имена инструментов (drift на переименование).
  4. Ручной слой резолвится (fail-loud работает: symbol+snippet на месте).
  5. Документ детерминирован (идемпотентность) и совпадает с зафиксированным на диске
     (= `--check` зелёный); подделанный документ ловится.
  6. Маркеры секций целы (ровно по одному).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GEN = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "gen_capabilities.py"


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_capabilities_test", _GEN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


GEN = _load_gen()


# --- 1 + 2. контрольный кейс #143 + golden-значения lookup_mode ------------
@pytest.mark.parametrize(
    "tool, expected",
    [
        ("complete_task", "id_only"),
        ("cancel_task", "id_only"),
        ("delete_task", "id_only"),
        ("list_tasks", "needs_prior_list"),
        # #143 Phase B: mark/delete по item_id (id из list_checklist_items).
        ("mark_checklist_item_done", "id_only"),
        ("delete_checklist_item", "id_only"),
        ("list_checklist_items", "needs_prior_list"),
        ("archive_checklist", "self_resolves_title"),
    ],
)
def test_lookup_mode_golden_values(tool, expected):
    rows = {t.name: t for t in GEN.collect_tools()}
    assert tool in rows, f"инструмент {tool} исчез из реестра"
    assert rows[tool].lookup_mode == expected, (
        f"{tool}: lookup_mode={rows[tool].lookup_mode!r}, ожидалось {expected!r}"
    )


def test_143_finder_find_by_title_present():
    # Контрольный кейс #143: finder уже есть — карта это показывает.
    finders = {f.name: f for f in GEN.collect_finders()}
    assert "find_by_title" in finders, "find_by_title пропал из object-finders"
    assert finders["find_by_title"].domain == "tasks"
    assert finders["find_by_title"].owner == "TaskService"


def test_143_checklist_finders_present():
    names = {f.name for f in GEN.collect_finders()}
    assert {"find_list_by_title", "find_item_by_title"} <= names


# --- 3. ключи LOOKUP_MODES — реальные инструменты --------------------------
def test_lookup_mode_keys_are_real_tools():
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS

    real = {s.name for s in MIGRATED_TOOL_SPECS}
    stray = set(GEN.LOOKUP_MODES) - real
    assert not stray, f"LOOKUP_MODES ссылается на несуществующие инструменты: {sorted(stray)}"


# --- 4. ручной слой резолвится (fail-loud работает) ------------------------
def test_manual_layer_anchors_resolve():
    mechs = GEN.load_manual()  # кинет AnchorError, если symbol/snippet ушли
    assert len(mechs) >= 5, "ожидалось 5-10 сквозных механизмов"
    for m in mechs:
        assert m.anchors, f"механизм {m.id} без якорей"
        for a in m.anchors:
            assert a.resolved_line > 0, f"{m.id}: якорь {a.symbol} не зарезолвлен"


def test_manual_layer_fails_loud_on_missing_snippet():
    # Подделанный сниппет → AnchorError (гарантия свежести ручного слоя).
    bad = GEN.Anchor(
        path="src/sreda/services/tasks.py",
        symbol="find_by_title",
        required_snippet="ЭТОГО-ТОЧНО-НЕТ-В-КОДЕ-12345",
    )
    with pytest.raises(GEN.AnchorError):
        GEN.resolve_anchor(bad)


def test_manual_layer_fails_loud_on_missing_symbol():
    bad = GEN.Anchor(
        path="src/sreda/services/tasks.py",
        symbol="no_such_symbol_xyz",
        required_snippet="x",
    )
    with pytest.raises(GEN.AnchorError):
        GEN.resolve_anchor(bad)


# --- 5. детерминизм + совпадение с диском (--check) -------------------------
def test_build_is_deterministic():
    assert GEN.build_document() == GEN.build_document()


def test_doc_on_disk_is_fresh():
    # = `python gen_capabilities.py --check` зелёный.
    assert GEN.DOC_PATH.exists(), "документ не сгенерирован"
    on_disk = GEN.DOC_PATH.read_text(encoding="utf-8")
    assert on_disk == GEN.build_document(), (
        "SREDA_CAPABILITIES.md устарел — перегенери: python scripts/dev/gen_capabilities.py"
    )


# Аудит 2026-07-18 (MINOR): on-disk документ лежит под gitignored
# docs/internal/ → на CI-checkout его НЕТ, и test_doc_on_disk_is_fresh
# там осознанно деселектится (ci-tests.yml) → до этого дня свежесть
# карты не была enforced НИ В ОДНОМ контуре, и документ на диске
# стабильно протухал (красный локальный свит → привыкание игнорировать).
# Контрольная сумма ниже КОММИТИТСЯ в репо вместе с этим тестом, поэтому
# drift «код ушёл, карта не перегенерирована» теперь ловится в CI на
# каждый PR. Обновление: `python scripts/dev/gen_capabilities.py`, затем
# подставить сюда новый хэш из упавшего ассерта (одна строка).
_DOC_SHA256 = "91a61fe8a61d5e2f172b7976f929b3168c2e3fda7348ed9aa38a5acd5a4bdfd3"


def test_doc_hash_pinned_for_ci():
    import hashlib

    got = hashlib.sha256(GEN.build_document().encode("utf-8")).hexdigest()
    assert got == _DOC_SHA256, (
        "карта возможностей рассинхронирована с кодом — перегенери "
        "`python scripts/dev/gen_capabilities.py` и обнови _DOC_SHA256 "
        f"в tests/unit/test_capabilities_map.py на {got}"
    )


def test_doctored_doc_is_detected():
    doctored = GEN.build_document().replace("id_only", "ВРУЧНУЮ-ПОДДЕЛАНО")
    assert doctored != GEN.build_document()


# --- 6. маркеры целы --------------------------------------------------------
def test_section_markers_intact():
    doc = GEN.build_document()
    for marker in (GEN.GEN_BEGIN, GEN.GEN_END, GEN.MAN_BEGIN, GEN.MAN_END):
        assert doc.count(marker) == 1, f"маркер встречается != 1 раза: {marker}"
