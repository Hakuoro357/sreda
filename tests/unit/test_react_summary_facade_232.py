"""#232 способ Б — фасад run_post_turn_summary: пишет выжимку в ТАБЛИЦУ react_summaries.

Реальная sqlite-сессия (для таблицы) + FakeGraph (для чтения messages) + подмена пересказчика.
Покрывает: durable-гейт, пропуск на паузе, базовый триггер, happy-path (запись в таблицу + учёт
task_type=summary), анти-петля, пере-сжатие на ДЕЛЬТЕ (промпт = prev + только новые сообщения).
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.runtime import react_loop as rl
from sreda.runtime import react_compaction as rc
from sreda.services import react_summary_store as store


def _hist(n_turns=12, pad=10):
    msgs = []
    for k in range(n_turns):
        msgs += [HumanMessage(f"вопрос{k} " * pad), AIMessage(f"ответ{k} " * pad)]
    return msgs


class _Snap:
    def __init__(self, values, paused=False):
        self.values = values
        self.tasks = []
        self.next = ("chat",) if paused else None
        self.created_at = None


class _FakeGraph:
    def __init__(self, snap):
        self._snap = snap

    async def aget_state(self, cfg):
        return self._snap

    async def aget_state_history(self, cfg):  # для _covered_through_ts → пустая история → None
        return
        yield  # noqa — делает функцию async-генератором


@pytest.fixture()
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SF
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def wired(monkeypatch, db):
    SF = db
    monkeypatch.setattr(rl, "_persist_enabled", lambda: True)
    monkeypatch.setattr(rl, "_summary_enabled_for", lambda tid: True)  # #232 канарейка-флаг: вкл для теста
    monkeypatch.setattr(rl, "_SUMMARY_RECOMPACT_LIMIT_CHARS", 1)  # #232 шаг C: лимит≈0 → гейт размера не мешает тестам ген/дельты
    monkeypatch.setattr(rl, "build_slice_tools", lambda *a, **k: [])
    monkeypatch.setattr(rl, "react_provider", lambda tid: "inception-mercury2")
    monkeypatch.setattr(rl, "_extract_usage", lambda resp: (10, 5))
    monkeypatch.setattr("sreda.db.session.get_session_factory", lambda: SF)
    monkeypatch.setattr("sreda.services.llm.get_chat_llm", lambda *a, **k: object())  # не None
    recorded, prompts = [], []

    def _fake_invoke(llm, msgs, **k):
        prompts.append(msgs)
        return AIMessage("ВЫЖИМКА: важные факты сохранены")

    monkeypatch.setattr(rl, "invoke_with_per_call_timeout", _fake_invoke)
    monkeypatch.setattr(rl, "_record_react_usage", lambda **kw: recorded.append(kw))
    return {"SF": SF, "recorded": recorded, "prompts": prompts}


def _run(thread_id="react:tenant_tg_1:1"):
    asyncio.run(rl.run_post_turn_summary(
        tenant_id="tenant_tg_1", user_id="u1", thread_id=thread_id, channel="telegram"))


def _key():
    return rl._durable_thread_id("react:tenant_tg_1:1")


def test_skip_when_not_persist(monkeypatch, wired):
    monkeypatch.setattr(rl, "_persist_enabled", lambda: False)
    built = []
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: built.append(1))
    _run()
    assert built == []
    assert store.load_summary(wired["SF"](), _key()) is None


def test_skip_when_not_enrolled(monkeypatch, wired):
    """#232 канарейка: тенант НЕ в SREDA_REACT_SUMMARY_TENANTS → выжимка не генерится (фича OFF)."""
    monkeypatch.setattr(rl, "_summary_enabled_for", lambda tid: False)
    built = []
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: built.append(1))
    _run()
    assert built == []
    assert store.load_summary(wired["SF"](), _key()) is None  # ничего не записано


def test_skip_on_pause(monkeypatch, wired):
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": _hist()}, paused=True)))
    _run()
    assert store.load_summary(wired["SF"](), _key()) is None


def test_skip_when_too_little_history(monkeypatch, wired):
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": _hist(n_turns=2)})))
    _run()
    assert store.load_summary(wired["SF"](), _key()) is None


def test_happy_path_writes_to_table(monkeypatch, wired):
    msgs = _hist(n_turns=12)
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    rec = store.load_summary(wired["SF"](), _key())
    assert rec is not None
    covered_n, _ = rc.summary_coverage(msgs)
    assert rec["covered_message_count"] == covered_n
    assert rec["covered_hash"] == rc._history_prefix_hash(msgs, covered_n)
    assert rec["version"] == rc._SUMMARY_VERSION
    assert "ВЫЖИМКА" in rec["text"]
    # учёт записан как summary (виден в стоимости, квоту не блокирует)
    assert wired["recorded"] and wired["recorded"][0]["task_type"] == "summary"


def test_anti_loop_already_covered(monkeypatch, wired):
    msgs = _hist(n_turns=12)
    covered_n, _ = rc.summary_coverage(msgs)
    s0 = wired["SF"]()
    store.upsert_summary(s0, thread_id=_key(), tenant_id="tenant_tg_1", text="СТАРАЯ выжимка",
                         covered_message_count=covered_n, covered_hash="x", version=1)
    s0.commit()
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    rec = store.load_summary(wired["SF"](), _key())
    assert rec["text"] == "СТАРАЯ выжимка"  # покрыто не меньше → не переписали


def test_recompression_uses_delta(monkeypatch, wired):
    """При наличии prev пересказчику подаётся prev + ТОЛЬКО новые сообщения (дельта), не весь префикс."""
    msgs = _hist(n_turns=12)
    covered_n, _ = rc.summary_coverage(msgs)
    prev_n = 4
    s0 = wired["SF"]()
    store.upsert_summary(s0, thread_id=_key(), tenant_id="tenant_tg_1", text="ПРЕДЫДУЩАЯ-ВЫЖИМКА",
                         covered_message_count=prev_n, covered_hash=rc._history_prefix_hash(msgs, prev_n),
                         version=1)  # ВАЛИДНЫЙ hash → M3 принимает prev → дельта включается
    s0.commit()
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    # новая запись покрывает covered_n (> prev_n) → пере-сжали
    rec = store.load_summary(wired["SF"](), _key())
    assert rec["covered_message_count"] == covered_n
    # промпт: есть предыдущая выжимка + первые prev_n сообщений НЕ переданы (дельта)
    human_text = wired["prompts"][0][1].content
    assert "ПРЕДЫДУЩАЯ-ВЫЖИМКА" in human_text
    assert "вопрос0 " not in human_text  # сообщение из уже покрытого префикса — не в дельте


def test_recompression_ignores_invalid_prev(monkeypatch, wired):
    """R2 MAJOR: prev с НЕВЕРНЫМ hash (стылая/битая) НЕ используется для дельты — пересказываем полный префикс."""
    msgs = _hist(n_turns=12)
    s0 = wired["SF"]()
    store.upsert_summary(s0, thread_id=_key(), tenant_id="tenant_tg_1", text="БИТАЯ-ВЫЖИМКА",
                         covered_message_count=4, covered_hash="неверный", version=1)
    s0.commit()
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    human_text = wired["prompts"][0][1].content
    assert "БИТАЯ-ВЫЖИМКА" not in human_text  # битый prev отвергнут (не «отмыт» в дельту)
    assert "вопрос0 " in human_text           # полный префикс с начала (дельта не включилась)


def test_stepC_freeze_first_summary_below_limit(monkeypatch, wired):
    """#232 шаг C: первая выжимка НЕ генерится, пока размер переписки ниже лимита (заморозка)."""
    monkeypatch.setattr(rl, "_SUMMARY_RECOMPACT_LIMIT_CHARS", 10 ** 9)  # лимит огромный → размер не влезает
    msgs = _hist(n_turns=12)  # ~2000 символов << лимита
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    assert store.load_summary(wired["SF"](), _key()) is None  # заморожено — выжимки нет
    assert not wired["prompts"]  # пересказчик НЕ вызывался


def test_stepC_fires_above_limit(monkeypatch, wired):
    """#232 шаг C: размер переписки превысил лимит → выжимка генерится."""
    monkeypatch.setattr(rl, "_SUMMARY_RECOMPACT_LIMIT_CHARS", 50)  # маленький лимит
    msgs = _hist(n_turns=12)  # ~2000 символов > 50
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    assert store.load_summary(wired["SF"](), _key()) is not None  # лимит превышен → выжимка есть


class _EnumFinish:
    """Enum-подобное значение finish_reason (native-обёртки могут класть не-строку)."""
    name = "STOP"
    value = 1


def _guard_invoke(monkeypatch, wired, msgs, **msg_kwargs):
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    monkeypatch.setattr(
        rl, "invoke_with_per_call_timeout",
        lambda llm, m, **k: AIMessage(content="ОБРЫВОК-ИЛИ-ВЫЖИМКА: перв", **msg_kwargs))


@pytest.mark.parametrize("msg_kwargs", [
    {"response_metadata": {"finish_reason": "error"}},           # live 2026-07-01 (обрывок, 451 симв)
    {"response_metadata": {"finish_reason": "length"}},          # обрезан по max_tokens
    {"response_metadata": {"finish_reason": "content_filter"}},  # OpenRouter-нормализация SAFETY
    {"response_metadata": {"finish_reason": "SAFETY"}},          # native-Google регистр (R1: нормализация)
    {"response_metadata": {"finish_reason": "PROHIBITED_CONTENT"}},  # gemini-native отказ (R2 high)
    {"response_metadata": {"stop_reason": "max_tokens"}},        # anthropic-стиль ключ (R1 high-2)
    {"additional_kwargs": {"finish_reason": "error"}},           # нестандартное место (R1 medium-2)
])
def test_guard_bad_finish_not_persisted(monkeypatch, wired, msg_kwargs, caplog):
    """#287: аварийный finish/stop_reason (обрывок с НЕПУСТЫМ контентом) → выжимка НЕ записана,
    но расход УЧТЁН (R1 sub: недоучёт в деградации; нулевой usage no-op'ится в _record_react_usage)."""
    _guard_invoke(monkeypatch, wired, _hist(n_turns=12), **msg_kwargs)
    with caplog.at_level("WARNING", logger=rl.logger.name):
        _run()
    assert store.load_summary(wired["SF"](), _key()) is None  # fail-open на #194, не персистим
    assert wired["recorded"] and wired["recorded"][0]["task_type"] == "summary"  # расход виден в money
    # R1-суб MAJOR (наблюдаемость петли скипов): warning несёт thread=durable_key — пин против рефакторинга
    skip_logs = [r.getMessage() for r in caplog.records if "skip persist finish_reason=" in r.getMessage()]
    assert skip_logs and f"thread={_key()}" in skip_logs[0]


@pytest.mark.parametrize("msg_kwargs", [
    {"response_metadata": {"finish_reason": "stop"}},            # openai/openrouter норма
    {"response_metadata": {"finish_reason": "STOP"}},            # native-Google успех (R1: не false-skip)
    {"response_metadata": {"stop_reason": "end_turn"}},          # anthropic-стиль успех
    {"response_metadata": {"finish_reason": _EnumFinish()}},     # enum-подобный успех (unwrap .name)
])
def test_guard_good_finish_persisted(monkeypatch, wired, msg_kwargs):
    """#287: успешный финиш в любом известном написании → персистим (метаданные не ломают happy-path)."""
    _guard_invoke(monkeypatch, wired, _hist(n_turns=12), **msg_kwargs)
    _run()
    rec = store.load_summary(wired["SF"](), _key())
    assert rec is not None and "перв" in rec["text"]


def test_guard_unknown_finish_persists(monkeypatch, wired):
    """#287: НЕИЗВЕСТНОЕ значение finish_reason → персистим + warning (fail-open: молчаливый вечный
    скип на новом значении провайдера хуже, чем прежнее поведение). Exemplar вымышленный (R2 суб:
    реальные значения классифицируются явно, pause_turn = BAD)."""
    _guard_invoke(monkeypatch, wired, _hist(n_turns=12),
                  response_metadata={"finish_reason": "new_reason_xyz"})
    _run()
    assert store.load_summary(wired["SF"](), _key()) is not None


def test_guard_missing_finish_reason_persisted(monkeypatch, wired):
    """#287: провайдер НЕ отдал finish_reason → персистим (fail-open к прежнему поведению;
    не ломаем пересказчик на провайдерах без этого поля). Дублирует happy-path осознанно —
    пинит именно выбор «отсутствие метаданных ≠ авария»."""
    msgs = _hist(n_turns=12)
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    # дефолтный _fake_invoke из wired уже без response_metadata — просто убеждаемся, что записано
    _run()
    assert store.load_summary(wired["SF"](), _key()) is not None


def test_guard_error_keeps_valid_prev_untouched(monkeypatch, wired):
    """#287 (R1 medium-3/sub-4): bad finish при СУЩЕСТВУЮЩЕЙ валидной prev-выжимке → prev остаётся
    побайтно прежней (ключевой инвариант заморозки: до гейта ничего не мутируется)."""
    msgs = _hist(n_turns=12)
    prev_n = 4
    s0 = wired["SF"]()
    store.upsert_summary(s0, thread_id=_key(), tenant_id="tenant_tg_1", text="ПРЕЖНЯЯ-ВЫЖИМКА",
                         covered_message_count=prev_n,
                         covered_hash=rc._history_prefix_hash(msgs, prev_n), version=1)
    s0.commit()
    _guard_invoke(monkeypatch, wired, msgs, response_metadata={"finish_reason": "error"})
    _run()
    rec = store.load_summary(wired["SF"](), _key())
    assert rec["text"] == "ПРЕЖНЯЯ-ВЫЖИМКА"
    assert rec["covered_message_count"] == prev_n
    assert rec["covered_hash"] == rc._history_prefix_hash(msgs, prev_n)


def test_stepC_freeze_with_prev_keeps_coverage(monkeypatch, wired):
    """#232 шаг C (ядро кеш-стабильности): есть prev-выжимка, переписка подросла, но размер ниже лимита →
    НЕ пере-сжимаем (covered_n и текст те же → префикс стабилен между ходами)."""
    monkeypatch.setattr(rl, "_SUMMARY_RECOMPACT_LIMIT_CHARS", 10 ** 9)
    msgs = _hist(n_turns=12)
    prev_n = 4
    s0 = wired["SF"]()
    store.upsert_summary(s0, thread_id=_key(), tenant_id="tenant_tg_1", text="ЗАМОРОЖЕННАЯ-ВЫЖИМКА",
                         covered_message_count=prev_n,
                         covered_hash=rc._history_prefix_hash(msgs, prev_n), version=1)
    s0.commit()
    monkeypatch.setattr(rl, "_build_graph", lambda *a, **k: _FakeGraph(_Snap({"messages": msgs})))
    _run()
    rec = store.load_summary(wired["SF"](), _key())
    assert rec["covered_message_count"] == prev_n          # НЕ пере-сжали → covered_n тот же (заморозка)
    assert rec["text"] == "ЗАМОРОЖЕННАЯ-ВЫЖИМКА"            # выжимка та же
    assert not wired["prompts"]                            # пересказчик НЕ вызывался
