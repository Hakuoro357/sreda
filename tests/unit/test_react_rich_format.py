# -*- coding: utf-8 -*-
"""Богатое форматирование ответа в Telegram — ТОЛЬКО для канареечных тенантов, за флагом.

Три слоя, которые сейчас физически не дают Среде ответить красиво, и что с ними делаем:
  1. промпт `<style>` запрещает markdown → для канарейки даём короткую инструкцию с примером;
  2. `_postformat` срезает разметку (`_strip_md`), пересобирает списки (`_format_lists`) и
     схлопывает переводы строк (`_scrub_ids`) → для канарейки эти три не применяем;
  3. отправка идёт без `parse_mode` → для канарейки включаем Markdown + ОБЯЗАТЕЛЬНЫЙ откат.

ГЛАВНЫЕ тесты здесь — «не в списке / пусто → байт-в-байт как сейчас» (ПРАВИЛО #1) и
«битая разметка → ответ всё равно доставлен обычным текстом» (ответ терять нельзя).

RED до фикса.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from sreda.config import settings as st_mod
from sreda.integrations.telegram.client import TelegramDeliveryError
from sreda.runtime import react_loop
from sreda.services import telegram_inbound as inbound_mod

_CANARY = "tenant_tg_352612382"
_OTHER = "tenant_tg_999"

# Разметка, которую мы хотим уметь отдавать (образец прототипа).
_RICH_REPLY = "*🎬 Кино к просмотру:*\n1. Машина смерти\n2. Твин Пикс"


class _Stub:
    """Минимальный дубль настроек для гейта (module-state патчим monkeypatch'ем)."""

    def __init__(self, tenants: set[str]) -> None:
        self.react_rich_format_tenants = frozenset(tenants)


# ────────────────────────── 1. настройка + гейт ──────────────────────────


def test_rich_format_tenants_settings_parse(monkeypatch):
    # env-парсинг allowlist'а (по образцу react_mimo_tenants)
    monkeypatch.setenv("SREDA_REACT_RICH_FORMAT_TENANTS", "tenant_a, tenant_b ,")
    assert st_mod.Settings().react_rich_format_tenants == frozenset({"tenant_a", "tenant_b"})


def test_rich_format_tenants_default_empty(monkeypatch):
    monkeypatch.delenv("SREDA_REACT_RICH_FORMAT_TENANTS", raising=False)
    monkeypatch.delenv("sreda_react_rich_format_tenants", raising=False)
    assert st_mod.Settings().react_rich_format_tenants == frozenset()


def test_gate_on_for_listed_tenant_in_telegram(monkeypatch):
    monkeypatch.setattr(st_mod, "get_settings", lambda: _Stub({_CANARY}))
    assert react_loop.rich_format_enabled(_CANARY, "telegram") is True


def test_gate_off_for_other_tenant(monkeypatch):
    # ГЛАВНЫЙ: не-флагнутый тенант → прежнее поведение, ноль изменений для прода
    monkeypatch.setattr(st_mod, "get_settings", lambda: _Stub({_CANARY}))
    assert react_loop.rich_format_enabled(_OTHER, "telegram") is False


def test_gate_off_when_flag_empty(monkeypatch):
    # ГЛАВНЫЙ: пустая настройка → никого не затронуло (ПРАВИЛО #1)
    monkeypatch.setattr(st_mod, "get_settings", lambda: _Stub(set()))
    assert react_loop.rich_format_enabled(_CANARY, "telegram") is False
    assert react_loop.rich_format_enabled("anyone", "telegram") is False


@pytest.mark.parametrize("channel", ["max", "react", "", "MAX"])
def test_gate_off_outside_telegram(monkeypatch, channel):
    # Scope — ТОЛЬКО Telegram: у MAX своя разметка, там parse_mode мы не шлём,
    # и звёздочки уехали бы юзеру как есть. Один тенант живёт в нескольких каналах.
    monkeypatch.setattr(st_mod, "get_settings", lambda: _Stub({_CANARY}))
    assert react_loop.rich_format_enabled(_CANARY, channel) is False


# ────────────────────────── 2. промпт ──────────────────────────


def test_default_prompt_still_bans_markdown():
    # регрессия для ВСЕХ остальных: запрет на месте, дефолтный промпт не тронут
    sp = react_loop._system_prompt("2030-01-01 (Вторник)")
    assert "Без markdown-звёздочек" in sp
    assert "🎬" not in sp


def test_rich_prompt_replaces_ban_with_example():
    sp = react_loop._system_prompt("2030-01-01 (Вторник)", rich_format=True)
    assert "Без markdown-звёздочек" not in sp, "в rich-режиме запрет обязан уйти"
    # инструкция несёт ПРИМЕР желаемого формата: заголовок одинарной звёздочкой + эмодзи
    assert "*🎬" in sp
    # двойные звёздочки Telegram Markdown v1 не понимает — модели это сказано явно
    assert "**" in sp and "НИКОГДА" in sp


def test_rich_prompt_stays_short():
    # промпт и так ~27 КБ; блок форматирования не должен его раздувать
    base = react_loop._system_prompt("2030-01-01 (Вторник)")
    rich = react_loop._system_prompt("2030-01-01 (Вторник)", rich_format=True)
    assert len(rich) - len(base) < 700, "rich-блок раздулся"


# ────────────────────────── 3. пост-обработка ──────────────────────────


def test_postformat_default_unchanged():
    # регрессия #168: дефолт срезает разметку и пересобирает списки — как сейчас
    assert react_loop._postformat("**Ингредиенты**") == "Ингредиенты"
    assert react_loop._postformat("# Заголовок\nтекст") == "Заголовок\nтекст"
    # нумерованная последовательность 1..N дробится _format_lists построчно
    out = react_loop._postformat("Шаги: 1. Нарежьте. 2. Обжарьте. 3. Подавайте.")
    assert out.count("\n") >= 3


def test_postformat_rich_keeps_bold():
    out = react_loop._postformat("*🎬 Кино:*\n1. Машина смерти", rich=True)
    assert out == "*🎬 Кино:*\n1. Машина смерти"


def test_postformat_rich_normalizes_double_asterisk():
    # Mercury исторически шлёт `**жирный**` вопреки промпту (#168). Telegram Markdown v1
    # такое не парсит → откат на голый текст И видимые `**`. Нормализуем в одинарные.
    assert react_loop._postformat("**Кино**", rich=True) == "*Кино*"


def test_postformat_rich_keeps_blank_line_between_sections():
    # пустая строка между разделами — часть желаемой вёрстки; сейчас её съедает _scrub_ids
    src = "*🥛 Молочные:*\n• молоко\n\n*🍞 Хлеб:*\n• хлеб"
    assert react_loop._postformat(src, rich=True) == src


def test_postformat_rich_does_not_rebuild_lists():
    # _format_lists (кустарная пересборка списков) в rich-режиме не применяется —
    # вёрстку задаёт модель по промпту, а не наш регексп
    out = react_loop._postformat("Шаги: 1. Нарежьте. 2. Обжарьте. 3. Подавайте.", rich=True)
    assert "\n" not in out


def test_postformat_rich_still_scrubs_tech_leaks():
    # безопасность не ослабляем: id/ref/okv2 не должны утечь и в rich-режиме
    out = react_loop._postformat("Готово (okv2:ok) id=abc123 rem_0123456789abcdef", rich=True)
    assert "okv2" not in out and "id=" not in out and "rem_" not in out


def test_postformat_rich_strips_hash_headers():
    # Telegram заголовков `#` не рисует — снимаем их и в rich-режиме
    assert react_loop._postformat("# Кино\nтекст", rich=True) == "Кино\nтекст"


# ────────────────────────── 4. проводка в handle_turn ──────────────────────────


class _Chat:
    """Фейк планировщика: финальный ответ — с разметкой."""

    def __init__(self, reply: str, seen_msgs: list) -> None:
        self._reply = reply
        self._seen = seen_msgs

    async def ainvoke(self, _msgs):
        return AIMessage(content="task")

    def bind_tools(self, _tools):
        outer = self

        def _inv(msgs):
            outer._seen.append(list(msgs))
            return AIMessage(content=outer._reply)
        return RunnableLambda(_inv)


@pytest.fixture
def react_env(monkeypatch):
    def _install(rich_tenants: str):
        monkeypatch.setenv("SREDA_REACT_RICH_FORMAT_TENANTS", rich_tenants)
        st_mod.get_settings.cache_clear()
        monkeypatch.setattr(
            react_loop, "build_slice_tools",
            lambda *a, **k: [StructuredTool.from_function(
                func=lambda q: "ok", name="need_family", description="Добрать семью.")],
        )
    yield _install
    st_mod.get_settings.cache_clear()


def _turn(llm, *, tenant, channel, thread):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id=tenant, user_id="u", thread_id=thread,
        llm=llm, user_text="покажи список кино", inbound_message_id=thread,
        channel=channel, provider_key="inception-mercury2"))


def test_turn_for_canary_keeps_markdown(react_env):
    react_env(_CANARY)
    seen: list = []
    reply = _turn(_Chat(_RICH_REPLY, seen), tenant=_CANARY, channel="telegram",
                  thread="rich-on")
    assert str(reply) == _RICH_REPLY, "разметка канарейки не должна срезаться"
    assert "*🎬" in seen[0][0].content, "канарейке ушёл rich-промпт"


def test_turn_for_other_tenant_byte_identical(react_env):
    # ГЛАВНЫЙ: тенант не в списке — прежнее поведение (разметка срезана, промпт с запретом)
    react_env(_CANARY)
    seen: list = []
    reply = _turn(_Chat("**Кино:**\nМашина смерти", seen), tenant=_OTHER,
                  channel="telegram", thread="rich-off")
    assert "*" not in str(reply)
    assert "Без markdown-звёздочек" in seen[0][0].content


def test_turn_for_canary_in_max_is_untouched(react_env):
    # тот же тенант в MAX — прежнее поведение (scope только Telegram)
    react_env(_CANARY)
    seen: list = []
    reply = _turn(_Chat("**Кино:**\nМашина смерти", seen), tenant=_CANARY,
                  channel="max", thread="rich-max")
    assert "*" not in str(reply)
    assert "Без markdown-звёздочек" in seen[0][0].content


# ────────────────────────── 5. отправка + откат ──────────────────────────


class _FakeTg:
    """Фейк Telegram-клиента: копит вызовы, по сценарию кидает ошибку."""

    def __init__(self, fail_first: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_first = fail_first

    async def _record(self, method: str, **kw):
        self.calls.append({"method": method, **kw})
        if self._fail_first is not None and len(self.calls) == 1:
            raise self._fail_first
        return {"ok": True}

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        return await self._record("send", chat_id=chat_id, text=text,
                                  parse_mode=parse_mode, reply_markup=reply_markup)

    async def edit_message_text(self, *, chat_id, message_id, text,
                                reply_markup=None, parse_mode=None):
        return await self._record("edit", chat_id=chat_id, message_id=message_id,
                                  text=text, reply_markup=reply_markup, parse_mode=parse_mode)


def _md_reject(status: int) -> TelegramDeliveryError:
    return TelegramDeliveryError("can't parse entities", method="sendMessage",
                                 status_code=status)


@pytest.mark.parametrize("status", [400, 200])
def test_send_falls_back_to_plain_on_broken_markdown(status):
    # Telegram отвергает битую разметку двумя формами: HTTP 400 и 200 с ok=false.
    # В ОБОИХ случаях ответ НЕ доставлен → повторяем без parse_mode, ответ не теряем.
    tg = _FakeTg(fail_first=_md_reject(status))
    asyncio.run(inbound_mod._tg_send_with_md_fallback(
        tg, chat_id="1", text=_RICH_REPLY, reply_markup=None, parse_mode="Markdown"))
    assert [c["parse_mode"] for c in tg.calls] == ["Markdown", None]
    assert tg.calls[-1]["text"] == _RICH_REPLY, "текст ответа обязан дойти целиком"


@pytest.mark.parametrize("status", [400, 200])
def test_edit_falls_back_to_plain_on_broken_markdown(status):
    tg = _FakeTg(fail_first=_md_reject(status))
    asyncio.run(inbound_mod._tg_edit_with_md_fallback(
        tg, chat_id="1", message_id=7, text=_RICH_REPLY,
        reply_markup=None, parse_mode="Markdown"))
    assert [c["parse_mode"] for c in tg.calls] == ["Markdown", None]
    assert tg.calls[-1]["text"] == _RICH_REPLY


def test_no_retry_on_network_failure():
    # таймаут/сеть (status_code=None) — неизвестно, дошло ли; ЕЩЁ ОДИН повтор от нас дал бы
    # ДУБЛЬ сообщения. Такую ошибку пробрасываем наверх, где уже есть свой фолбэк.
    # ⚠️ Тест про ХЕЛПЕР, а не про весь тракт: сам TelegramClient._post_request и так делает
    # до трёх попыток на timeout/5xx (поведение прода ДО этой задачи, для всех тенантов, мы
    # его не трогаем). Здесь фиксируем только то, что откат по разметке НЕ добавляет к этому
    # ещё одну попытку. Сужение ретраев клиента — отдельная задача (R1 sol MAJOR#1).
    tg = _FakeTg(fail_first=TelegramDeliveryError("timeout", method="sendMessage"))
    with pytest.raises(TelegramDeliveryError):
        asyncio.run(inbound_mod._tg_send_with_md_fallback(
            tg, chat_id="1", text="привет", reply_markup=None, parse_mode="Markdown"))
    assert len(tg.calls) == 1


def test_without_parse_mode_client_called_exactly_as_before():
    # не-канарейка: хелпер обязан звать клиента БЕЗ parse_mode — байт-в-байт как сейчас
    tg = _FakeTg()
    asyncio.run(inbound_mod._tg_send_with_md_fallback(
        tg, chat_id="1", text="привет", reply_markup={"k": 1}, parse_mode=None))
    asyncio.run(inbound_mod._tg_edit_with_md_fallback(
        tg, chat_id="1", message_id=7, text="привет", reply_markup=None, parse_mode=None))
    assert [c["parse_mode"] for c in tg.calls] == [None, None]
    assert len(tg.calls) == 2, "лишних попыток быть не должно"


def test_all_react_reply_sites_go_through_fallback_helper():
    """Каждая точка доставки ответа ReAct обязана идти через хелпер с откатом.
    Прямой вызов клиента = потерянный ответ при битой разметке."""
    tree = ast.parse(Path(inbound_mod.__file__).read_text(encoding="utf-8"))
    direct, wrapped = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        text_kw = next((k for k in node.keywords if k.arg == "text"), None)
        if text_kw is None or not isinstance(text_kw.value, ast.Name):
            continue
        if text_kw.value.id not in ("_reply", "_reply2"):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in ("send_message", "edit_message_text"):
            direct.append(node.lineno)
        elif isinstance(fn, ast.Name) and fn.id in (
                "_tg_send_with_md_fallback", "_tg_edit_with_md_fallback"):
            wrapped.append(node.lineno)
    assert not direct, f"ответ ReAct шлётся мимо отката, строки: {direct}"
    assert len(wrapped) == 4, f"ожидали 4 точки доставки, нашли {len(wrapped)}: {wrapped}"


def test_client_edit_message_text_accepts_parse_mode():
    # оснастка транспорта: editMessageText обязан уметь parse_mode (send_message уже умеет)
    from sreda.integrations.telegram.client import TelegramClient
    assert "parse_mode" in inspect.signature(TelegramClient.edit_message_text).parameters
