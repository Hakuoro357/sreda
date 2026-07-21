"""Юнит-тесты чистых хелперов харнесса (без handle_turn): классификация квитанций."""
from __future__ import annotations

from .harness import _classify_receipt


def test_classify_add_created_applied():
    c = 'okv2:added:{"added_count":2,"duplicate_count":0,"created":["молоко","хлеб"]}'
    assert _classify_receipt("add_shopping_items", "ok", c) == "applied"


def test_classify_add_all_dup_already_applied():
    c = 'okv2:added_with_dups:{"added_count":0,"duplicate_count":2,"created":[]}'
    assert _classify_receipt("add_shopping_items", "ok", c) == "already_applied"


def test_classify_add_empty_noop():
    c = 'okv2:added:{"added_count":0,"duplicate_count":0,"created":[]}'
    assert _classify_receipt("add_shopping_items", "ok", c) == "noop"


def test_classify_zero_effect_contracts_are_noop():
    """R3 (ревью sol+terra R2): ok:updated:0 / ok:cleared:0 / not_found — эффекта нет."""
    for content in ("ok:updated:0", "ok:cleared:0", "ok:noop", "ok:cleared_or_not_found"):
        assert _classify_receipt("update_task", "ok", content) == "noop", content


def test_classify_target_success_applied():
    assert _classify_receipt("archive_checklist", "ok", "ok:archived:checklist_x") == "applied"


def test_classify_error_failed():
    assert _classify_receipt("archive_checklist", "error", "error:not_found") == "failed"
    assert _classify_receipt("archive_checklist", "ok", "Хорошо, не трогаю.") == "failed"


def test_classify_read_tool():
    assert _classify_receipt("get_checklist", "ok", "result_type=overview ...") == "read"
