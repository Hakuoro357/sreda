"""#410 — недельный разбор расхождений доменов (#376) вместо потока per-turn алертов.

Чек-лист приёмки #410:
- п.1 (per-turn алерт этого класса погашен, прочие целы) — ``test_per_turn_divergence_notifier_removed``,
  ``test_other_admin_alert_paths_intact``;
- п.2/3 (агрегация считает верно; ранжирование «что чинить первым») —
  ``test_gather_counts_aggregates_divergences``, ``test_ranking_puts_unapplied_first``;
- п.4 (пустая неделя → внятный отчёт) — ``test_empty_week_report_is_meaningful``,
  ``test_report_when_mechanism_never_ran``;
- п.5 (в отчёт не текут сырые ПД) — ``test_scrub_phrase_removes_pii``,
  ``test_examples_in_report_are_scrubbed``;
- каденс раз в неделю + откат после провала — ``test_runs_once_per_iso_week``,
  ``test_backoff_after_failure``.

RED до реализации: модуль ``sreda.workers.domain_divergence_digest`` ещё не существует.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.react_trace import ReactTurnTrace
from sreda.workers import domain_divergence_digest as dd
from sreda.workers.domain_divergence_digest import _scan_scope as _real_scan_scope
from sreda.workers.domain_divergence_digest import (
    DomainDivergenceDigestWorker,
    format_report,
    gather_week_counts,
    scrub_phrase,
)

NOW = datetime(2026, 7, 21, 3, 0, tzinfo=timezone.utc)  # понедельник, ISO-неделя 30
TENANT = "tenant_max_40921122"


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture(autouse=True)
def _allow_previews(monkeypatch):
    """По умолчанию в тестах тенант-канарейка В privacy-allowlist — иначе формулировок
    не будет вовсе (CR R1: показывать тексты можно только явно разрешённым тенантам).
    Тесты про запрет переопределяют фикстуру своим monkeypatch."""
    monkeypatch.setattr(dd, "_preview_tenants", lambda: frozenset({TENANT}))
    monkeypatch.setattr(dd, "_scan_scope", lambda: ("all", []))


_seq = iter(range(100000))


def _trace(sess, *, disambig: dict | None, days_ago: float = 1.0,
           text: str | None = None, tenant: str = TENANT):
    """Строка react_turn_trace с блоком routing_decision_json.disambig — ровно той формы,
    что пишет react_loop (`_rdj["disambig"] = _dis376`, react_loop.py:5615)."""
    n = next(_seq)
    rdj: dict = {"mode": "unified-execute", "allowed_read": ["checklists"],
                 "allowed_write": [], "signals": {}}
    if disambig is not None:
        rdj["disambig"] = disambig
    sess.add(ReactTurnTrace(
        id=f"rtt410_{n}", tenant_id=tenant, turn_key=f"tk410_{n}",
        status="done", outcome="ok",
        origin_user_text=text,
        routing_decision_json=json.dumps(rdj, ensure_ascii=False),
        created_at=NOW - timedelta(days=days_ago)))
    return f"rtt410_{n}"


def _sub_applied(**kw) -> dict:
    """Вердикт применён: вычли лишних членов кюс-группы (статик был шире)."""
    return {"ran": True, "duration_ms": 900, "confidence": "high",
            "static_domains": ["checklists", "shopping"],
            "freddie_domains": ["checklists"],
            "kind": "subtract", "applied": True, **kw}


def _add_not_applied(**kw) -> dict:
    """Вердикт ВНЕ поднятых доменов — не применяем (анти-инъекция), но это расхождение."""
    return {"ran": True, "duration_ms": 800, "confidence": "high",
            "static_domains": ["checklists"],
            "freddie_domains": ["shopping"],
            "kind": "add", "applied": False, **kw}


def _agreement(**kw) -> dict:
    """Согласие: вердикт совпал со статиком, политика не менялась — НЕ расхождение."""
    return {"ran": True, "duration_ms": 700, "confidence": "high",
            "static_domains": ["checklists"],
            "freddie_domains": ["checklists"],
            "kind": "subtract", "applied": False, **kw}


# ───────────────────────── агрегация (п.2/3 чек-листа) ─────────────────────────


def test_gather_counts_aggregates_divergences(session):
    """Расхождение = kind=='add' ИЛИ applied — тот же предикат, что гасил per-turn алерт
    (react_loop.py:5520). Согласие, чужое окно и ходы без блока — вне числа расхождений."""
    for _ in range(3):
        _trace(session, disambig=_sub_applied(), text="что у меня в списке кино")
    for _ in range(2):
        _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    for _ in range(4):
        _trace(session, disambig=_agreement(), text="покажи список дел")
    _trace(session, disambig={"ran": True, "error": True})          # сбой дизамбигуации
    _trace(session, disambig=_sub_applied(), days_ago=9.0)          # вне окна недели
    _trace(session, disambig={"ran": False})                        # гейт не отработал
    _trace(session, disambig=None)                                  # ход без #376 вообще
    session.commit()

    c = gather_week_counts(session, now=NOW)

    assert c.turns_with_disambig == 10, "ran=True за окно: 3+2+4+1"
    assert c.divergences == 5, "3 применённых вычитания + 2 неприменённых добавления"
    assert c.subtract_applied == 3
    assert c.add_not_applied == 2
    assert c.disambig_errors == 1
    sigs = {(x.kind, x.static_domains, x.freddie_domains, x.count) for x in c.cases}
    assert sigs == {
        ("subtract", ("checklists", "shopping"), ("checklists",), 3),
        ("add", ("checklists",), ("shopping",), 2),
    }


def test_ranking_puts_unapplied_first(session):
    """«Что чинить в первую очередь»: неприменённые расхождения (add) — впереди
    применённых, даже если тех больше: применённое механизм уже исправил сам,
    неприменённое ушло в ход как есть."""
    for _ in range(10):
        _trace(session, disambig=_sub_applied(), text="что в списке кино")
    _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    session.commit()

    c = gather_week_counts(session, now=NOW)

    assert [x.kind for x in c.cases] == ["add", "subtract"]
    assert c.cases[0].applied is False and c.cases[0].count == 1
    assert c.cases[1].applied is True and c.cases[1].count == 10


def test_cases_ranked_by_frequency_within_group(session):
    """Внутри группы (одинаковая применённость) — по частоте убыванием."""
    for _ in range(2):
        _trace(session, disambig=_sub_applied(), text="что в списке кино")
    for _ in range(5):
        _trace(session, disambig=_sub_applied(
            static_domains=["tasks", "reminders"], freddie_domains=["tasks"]),
            text="какие у меня задачи")
    session.commit()

    c = gather_week_counts(session, now=NOW)

    assert [x.count for x in c.cases] == [5, 2]


# ───────────────────────── формат отчёта (п.2/3/4) ─────────────────────────


def test_report_answers_what_to_fix_first(session):
    """Отчёт содержит: объём, разбивку по видам, ранжированный список «чинить первым»."""
    for _ in range(6):
        _trace(session, disambig=_sub_applied(), text="что у меня в списке кино")
    for _ in range(2):
        _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    session.commit()

    text = format_report(gather_week_counts(session, now=NOW), now=NOW)

    assert "8" in text                       # всего расхождений
    lines = text.splitlines()
    fix = next(i for i, ln in enumerate(lines) if "Чинить в первую очередь" in ln)
    done = next(i for i, ln in enumerate(lines) if "Вычтено классификатором" in ln)
    # секция «чинить» идёт ПЕРВОЙ и содержит неприменённое расхождение
    assert fix < done
    assert lines[fix + 1] == "1. checklists → shopping · ходов: 2"
    # применённые вычитания показаны отдельной секцией, не выдавлены из отчёта
    assert lines[done + 1] == "1. checklists,shopping → checklists · ходов: 6"


def test_empty_week_report_is_meaningful(session):
    """Неделя без расхождений: отчёт всё равно осмысленный — сколько ходов проверено
    и явное «расхождений нет», а НЕ пустое сообщение."""
    for _ in range(12):
        _trace(session, disambig=_agreement(), text="покажи список дел")
    session.commit()

    text = format_report(gather_week_counts(session, now=NOW), now=NOW)

    assert text.strip(), "отчёт не должен быть пустым"
    assert "12" in text, "должно быть видно, сколько ходов проверено"
    assert "расхождений нет" in text.lower()
    assert "Чинить в первую очередь" not in text


def test_report_when_mechanism_never_ran(session):
    """Ни одного хода с дизамбигуацией (флаг снят / тишина) — отчёт это ГОВОРИТ,
    а не выглядит как «всё хорошо»."""
    _trace(session, disambig=None, text="привет")
    session.commit()

    text = format_report(gather_week_counts(session, now=NOW), now=NOW)

    assert text.strip()
    assert "не срабатывал" in text.lower()


def test_report_mentions_disambig_errors(session):
    """Сбои самого классификатора — отдельной строкой (fail-open прячет их от исхода хода)."""
    _trace(session, disambig={"ran": True, "error": True})
    _trace(session, disambig=_sub_applied(), text="что в списке кино")
    session.commit()

    text = format_report(gather_week_counts(session, now=NOW), now=NOW)

    assert "сбо" in text.lower()


# ───────────────────────── ПД (п.5) ─────────────────────────


def test_scrub_phrase_removes_pii():
    """Обезличивание: цифры, почта, ссылки, @-хэндлы вычищаются; длина обрезается."""
    assert "89161234567" not in scrub_phrase("позвони на 89161234567")
    assert "@" not in scrub_phrase("напиши @vasya_petrov")
    assert "boris@example.com" not in scrub_phrase("отправь на boris@example.com")
    assert "http" not in scrub_phrase("открой https://secret.example.com/x")
    long = scrub_phrase("а " * 200)
    assert len(long) <= 80, "формулировка обрезается"
    assert scrub_phrase("  что   у меня\nв списке кино  ") == "что у меня в списке кино"
    assert scrub_phrase(None) == ""
    assert scrub_phrase("") == ""


def test_examples_in_report_are_scrubbed(session):
    """Примеры формулировок в отчёте — только обезличенные и ограниченные числом."""
    for i in range(9):
        _trace(session, disambig=_add_not_applied(),
               text=f"позвони маме на 8916123456{i} по поводу списка")
    session.commit()

    counts = gather_week_counts(session, now=NOW)
    text = format_report(counts, now=NOW)

    assert "89161234560" not in text
    assert len(counts.cases[0].examples) <= dd.EXAMPLES_PER_CASE
    for phrase, _n in counts.cases[0].examples:
        assert not any(ch.isdigit() for ch in phrase)


def test_examples_only_for_privacy_allowlisted_tenants(session, monkeypatch):
    """CR R1 (sol MAJOR / terra CRITICAL): скраббер не есть анонимизация — имена и
    адреса регуляркой не вычистить. Поэтому текст показывается ТОЛЬКО для тенантов из
    admin_alert_preview_tenants; по умолчанию список пуст → формулировок нет ни у кого,
    и расширение канарейки #376 на всех НЕ начинает лить чужие тексты владельцу."""
    monkeypatch.setattr(dd, "_preview_tenants", frozenset)  # пустой allowlist
    for _ in range(3):
        _trace(session, disambig=_add_not_applied(), text="Иванов Пётр, Ленина 5 кв 3")
    session.commit()

    counts = gather_week_counts(session, now=NOW)
    text = format_report(counts, now=NOW)

    assert counts.divergences == 3, "числа считаются по-прежнему"
    assert counts.cases[0].examples == (), "а тексты не показываются"
    assert "Иванов" not in text and "Ленина" not in text
    assert counts.examples_suppressed is True
    assert "privacy-allowlist" in text


def test_foreign_tenant_text_never_shown(session, monkeypatch):
    """Тенант вне allowlist не отдаёт формулировки, даже когда свой — отдаёт."""
    monkeypatch.setattr(dd, "_preview_tenants", lambda: frozenset({TENANT}))
    _trace(session, disambig=_add_not_applied(), text="своя фраза")
    _trace(session, disambig=_add_not_applied(), text="ЧУЖАЯ ТАЙНА", tenant="tenant_tg_777")
    session.commit()

    text = format_report(gather_week_counts(session, now=NOW), now=NOW)

    assert "своя фраза" in text
    assert "ЧУЖАЯ ТАЙНА" not in text and "тайна" not in text.lower()


def test_top_phrasings_ranked_by_frequency(session):
    """CR R1 sol MAJOR: «топ формулировок» — лидеры ЧАСТОТЫ, а не первые по времени.
    Редкие ранние фразы не должны вытеснять ту, что повторилась десятки раз."""
    for i in range(8):
        _trace(session, disambig=_add_not_applied(), days_ago=6.0,
               text=f"редкая фраза номер {i}")
    for _ in range(30):
        _trace(session, disambig=_add_not_applied(), days_ago=1.0,
               text="частая фраза про покупки")
    session.commit()

    counts = gather_week_counts(session, now=NOW)

    assert counts.cases[0].examples[0] == ("частая фраза про покупки", 30)
    assert "x30" in format_report(counts, now=NOW)


@pytest.mark.parametrize("bad", [
    pytest.param({"kind": "bogus", "applied": True,
                  "static_domains": ["a"], "freddie_domains": ["b"]}, id="kind-не-из-набора"),
    pytest.param({"kind": "add", "applied": "да",
                  "static_domains": ["a"], "freddie_domains": ["b"]}, id="applied-не-bool"),
    pytest.param({"kind": "add", "applied": True,
                  "static_domains": ["a"], "freddie_domains": ["b"]},
                 id="add+applied-невозможно"),
    pytest.param({"kind": None, "applied": True,
                  "static_domains": ["a"], "freddie_domains": ["b"]},
                 id="вердикта-нет-но-применено"),
    pytest.param({"kind": "add", "applied": False, "freddie_domains": ["b"]},
                 id="нет-static_domains"),
    pytest.param({"kind": "add", "applied": False, "static_domains": ["a"]},
                 id="нет-freddie_domains"),
    pytest.param({"kind": "add", "applied": False,
                  "static_domains": [{"oops": 1}], "freddie_domains": ["b"]},
                 id="нехешируемый-домен"),
])
def test_malformed_disambig_block_is_skipped_not_counted(session, bad):
    """CR R1+R2 (sol+terra MAJOR): любая битая/семантически невозможная форма блока
    считается malformed, а НЕ расхождением. Иначе разбивка и проценты врут, а
    нехешируемый домен вообще роняет весь недельный отчёт."""
    _trace(session, disambig={"ran": True, **bad})
    _trace(session, disambig=_sub_applied(), text="что в списке кино")
    session.commit()

    c = gather_week_counts(session, now=NOW)

    assert c.turns_with_disambig == 2
    assert c.divergences == 1, "битая строка не считается расхождением"
    assert c.malformed == 1
    assert "не той формы" in format_report(c, now=NOW)


def test_week_of_only_malformed_rows_does_not_claim_agreement(session):
    """CR R2 (sol+terra MAJOR): неделя, где ВСЕ строки битые, не должна рапортовать
    «статик и классификатор согласны» и прятать счётчик битых."""
    for _ in range(4):
        _trace(session, disambig={"ran": True, "kind": "bogus", "applied": True,
                                  "static_domains": ["a"], "freddie_domains": ["b"]})
    session.commit()

    c = gather_week_counts(session, now=NOW)
    text = format_report(c, now=NOW)

    assert c.divergences == 0 and c.malformed == 4
    assert "согласны" not in text
    assert "не той формы (пропущены): 4" in text


def test_ran_flag_must_be_true_bool(session):
    """Строка с ran не-True не засчитывается ходом дизамбигуации."""
    _trace(session, disambig={"ran": 1, "kind": "add", "applied": False,
                              "static_domains": [], "freddie_domains": ["b"]})
    session.commit()

    assert gather_week_counts(session, now=NOW).turns_with_disambig == 0


def test_scan_narrowed_to_disambig_tenants(session):
    """CR R1 terra MAJOR: при явном allowlist #376 недельный запрос сужается по тенанту,
    а не сканирует весь трейс окна по всем тенантам."""
    _trace(session, disambig=_add_not_applied(), text="своя")
    _trace(session, disambig=_add_not_applied(), text="чужая", tenant="tenant_tg_777")
    session.commit()

    c = gather_week_counts(session, now=NOW, scope=("list", [TENANT]))

    assert c.divergences == 1, "чужой тенант вне выборки"


def test_scope_none_skips_sql_entirely(session, monkeypatch):
    """CR R2 terra MAJOR: гейт #376 выключен → блоков disambig нет в принципе,
    и воркер не должен трогать БД (глобальный LIKE-скан ради нулевого отчёта)."""
    _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    session.commit()

    def _boom(*a, **kw):
        raise AssertionError("SQL не должен выполняться при выключенном гейте")
    monkeypatch.setattr(session, "execute", _boom)

    c = gather_week_counts(session, now=NOW, scope=("none", []))

    assert c.turns_with_disambig == 0 and c.divergences == 0
    assert "не срабатывал" in format_report(c, now=NOW).lower()


def test_scope_resolution_from_settings(monkeypatch):
    """Разбор гейта: выключен → none; пусто → none; список → list; «*» → all."""
    from sreda.config import settings as st

    def _fake(enabled, raw):
        return SimpleNamespace(
            domain_clf_disambig_enabled=enabled,
            domain_clf_disambig_tenants=st._parse_tenant_gate(raw))
    # _real_scan_scope импортирован до autouse-подмены — зовём настоящую реализацию
    monkeypatch.setattr(st, "get_settings", lambda: _fake(False, "t1"))
    assert _real_scan_scope() == ("none", []), "флаг выключен"
    monkeypatch.setattr(st, "get_settings", lambda: _fake(True, None))
    assert _real_scan_scope() == ("none", []), "список тенантов пуст"
    monkeypatch.setattr(st, "get_settings", lambda: _fake(True, "t1,t2"))
    assert _real_scan_scope() == ("list", ["t1", "t2"])
    monkeypatch.setattr(st, "get_settings", lambda: _fake(True, "*"))
    assert _real_scan_scope() == ("all", []), "режим «всем» отличается от «никому»"


def test_scope_union_survives_canary_change_mid_week():
    """CR R2 sol MAJOR: окно охватывает 7 прошедших дней, поэтому сегодняшний гейт не
    источник истины за весь период — смена канарейки внутри недели не должна терять
    расхождения прежнего тенанта."""
    assert dd._merge_scope(("list", ["t_new"]), "list", ["t_old"]) == \
        ("list", ["t_new", "t_old"])
    # гейт сняли совсем, но неделя ещё содержит ходы прежнего тенанта
    assert dd._merge_scope(("none", []), "list", ["t_old"]) == ("list", ["t_old"])
    # «всем» с любой стороны поглощает
    assert dd._merge_scope(("list", ["t1"]), "all", []) == ("all", [])
    assert dd._merge_scope(("all", []), "list", ["t1"]) == ("all", [])
    # ничего не было и нет → сканировать нечего
    assert dd._merge_scope(("none", []), None, None) == ("none", [])


def test_worker_records_gate_and_merges_next_run(tmp_path, monkeypatch):
    """Гейт прогона ложится в state и участвует в объединении на следующей неделе."""
    import asyncio
    import json as _json
    state_path = tmp_path / "state.json"
    w = DomainDivergenceDigestWorker(state_file=str(state_path))
    seen: list = []
    monkeypatch.setattr(dd, "send_admin_alert", lambda *a, **kw: None)
    monkeypatch.setattr(dd, "_scan_scope", lambda: ("list", ["t_old"]))
    monkeypatch.setattr(dd, "_gather_with_session", lambda now, scope=None: (
        seen.append(scope) or dd.WeekCounts(0, 0, 0, 0, 0)))

    asyncio.run(w.process_pending(now=NOW))
    assert _json.loads(state_path.read_text(encoding="utf-8"))["last_scan_tenants"] == ["t_old"]

    monkeypatch.setattr(dd, "_scan_scope", lambda: ("list", ["t_new"]))
    asyncio.run(w.process_pending(now=NOW + timedelta(days=7)))
    assert seen[-1] == ("list", ["t_new", "t_old"])


def test_sampled_frequencies_are_labelled(session, monkeypatch):
    """CR R2 (sol+terra MAJOR): если частоты посчитаны не по всем строкам случая,
    отчёт обязан это сказать, а не выдавать выборочное xN за недельную частоту."""
    monkeypatch.setattr(dd, "MAX_EXAMPLE_ROWS_PER_CASE", 2)
    for _ in range(5):
        _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    session.commit()

    c = gather_week_counts(session, now=NOW)

    assert c.cases[0].count == 5, "сам счётчик расхождений остаётся точным"
    assert c.cases[0].examples_sampled is True
    assert "(по выборке)" in format_report(c, now=NOW)


def test_full_frequencies_are_not_labelled_as_sample(session):
    """И наоборот: когда посчитано по всем строкам, метки выборки нет."""
    for _ in range(5):
        _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    session.commit()

    c = gather_week_counts(session, now=NOW)

    assert c.cases[0].examples == (("покажи покупки", 5),)
    assert c.cases[0].examples_sampled is False
    assert "(по выборке)" not in format_report(c, now=NOW)


def test_scrub_normalises_em_dash():
    """CR R1 terra MINOR: длинное тире не должно уезжать в исходящий текст."""
    out = scrub_phrase("купи хлеб — и молоко")
    assert "—" not in out and "–" not in out
    assert "-" in out


def test_example_fetch_failure_degrades_not_crashes(session, monkeypatch):
    """Сбой чтения зашифрованного текста (ключ/ротация) НЕ роняет отчёт —
    он уходит без примеров."""
    for _ in range(2):
        _trace(session, disambig=_add_not_applied(), text="покажи покупки")
    session.commit()

    def _boom(*a, **kw):
        raise RuntimeError("decrypt failed")
    monkeypatch.setattr(dd, "_fetch_example_texts", _boom)

    c = gather_week_counts(session, now=NOW)
    assert c.divergences == 2
    assert c.cases[0].examples == ()
    assert format_report(c, now=NOW).strip()


# ───────────── погашенный per-turn алерт этого класса (п.1) ─────────────


def test_per_turn_divergence_notifier_removed():
    """#410: расхождение доменов больше НЕ шлёт сообщение на каждом ходе —
    оно копится в трейсе и уходит недельным разбором."""
    from sreda.runtime import react_loop as rl

    assert not hasattr(rl, "_notify_domain_divergence"), \
        "per-turn нотификация расхождения должна быть удалена (#410)"
    assert not hasattr(rl, "_dis376_send_alert")
    assert not hasattr(rl, "_DIS376_SEEN")


def test_other_admin_alert_paths_intact():
    """Прочие классы служебных сообщений не задеты: общий дуал-канал жив и
    его используют другие воркеры."""
    import inspect

    from sreda.runtime import react_loop as rl
    from sreda.services import admin_alerts as aa
    from sreda.workers import reliability_report as rr

    # оба входа общего дуал-канала (TG+MAX) на месте — #410 их не трогал
    assert callable(aa.send_admin_alert) and callable(aa.alert_admin_async)
    # суточная сводка надёжности (#139) по-прежнему шлёт через тот же вход.
    # Сравнивать identity нельзя: autouse-фикстура conftest подменяет
    # aa.send_admin_alert, и что захватил from-import — зависит от порядка импортов.
    rr_src = inspect.getsource(rr)
    assert "from sreda.services.admin_alerts import send_admin_alert" in rr_src
    assert "send_admin_alert(" in rr_src
    assert "send_admin_alert(" in inspect.getsource(dd)
    # per-turn канал расхождения из react_loop убран целиком, не просто отключён
    rl_src = inspect.getsource(rl)
    assert "_notify_domain_divergence" not in rl_src
    assert "alert_admin_async" not in rl_src


# ───────────────────────── каденс и устойчивость ─────────────────────────


def _worker(tmp_path, monkeypatch, counts_stub=None):
    w = DomainDivergenceDigestWorker(state_file=str(tmp_path / "state.json"))
    sent: list = []
    monkeypatch.setattr(
        dd, "send_admin_alert",
        lambda sev, title, body, **kw: sent.append((sev, title, body)))
    monkeypatch.setattr(dd, "_gather_with_session",
                        counts_stub or (lambda now, scope=None: dd.WeekCounts(
                            turns_with_disambig=5, divergences=0,
                            subtract_applied=0, add_not_applied=0,
                            disambig_errors=0, cases=(), truncated=False)))
    return w, sent


def test_runs_once_per_iso_week(tmp_path, monkeypatch):
    """Раз в календарную неделю (ISO): второй прогон той же недели — молчит,
    следующая неделя — шлёт."""
    import asyncio
    w, sent = _worker(tmp_path, monkeypatch)

    assert asyncio.run(w.process_pending(now=NOW)) == 1
    assert asyncio.run(w.process_pending(now=NOW + timedelta(days=3))) == 0
    assert asyncio.run(w.process_pending(now=NOW + timedelta(days=7))) == 1
    assert len(sent) == 2


def test_backoff_after_failure(tmp_path, monkeypatch):
    """Провал сбора → откат (не шторм): повтор в окне backoff молчит."""
    import asyncio

    def _boom(now, scope=None):
        raise RuntimeError("db down")
    w, sent = _worker(tmp_path, monkeypatch, counts_stub=_boom)

    assert asyncio.run(w.process_pending(now=NOW)) == 0
    assert sent == []
    assert asyncio.run(w.process_pending(now=NOW + timedelta(minutes=5))) == 0


def test_delivery_uses_dual_channel_helper(tmp_path, monkeypatch):
    """Доставка — существующим дуал-каналом (send_admin_alert), со стабильным dedupe_key."""
    import asyncio
    w = DomainDivergenceDigestWorker(state_file=str(tmp_path / "state.json"))
    calls: list = []
    monkeypatch.setattr(dd, "send_admin_alert",
                        lambda sev, title, body, **kw: calls.append((sev, title, kw)))
    monkeypatch.setattr(dd, "_gather_with_session", lambda now, scope=None: dd.WeekCounts(
        turns_with_disambig=3, divergences=0, subtract_applied=0,
        add_not_applied=0, disambig_errors=0, cases=(), truncated=False))

    asyncio.run(w.process_pending(now=NOW))

    assert len(calls) == 1
    sev, _title, kw = calls[0]
    assert sev == "INFO"
    assert "dedupe_key" in kw and "2026" in kw["dedupe_key"]
