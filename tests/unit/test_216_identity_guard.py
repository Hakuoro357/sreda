# -*- coding: utf-8 -*-
"""#216 — гард личности: ответ не должен раскрывать базовую модель/провайдера."""
from __future__ import annotations

import pytest

import sreda.runtime.react_loop as rl


# #216 гард ОТКЛЮЧЁН (issue follow-up): давал ложные срабатывания — нюкал ЛЮБОЙ ответ,
# где бренд модели встретился в КОНТЕНТЕ (живой инцидент: ответ про AI-новости с
# «Jack Clark (экс-OpenAI)» схлопнулся в identity-заглушку у реального юзера).
# Теперь _postformat НЕ редактирует; от само-раскрытия защищает промпт <identity>.
# Функция _redact_identity оставлена (не вызывается) на случай возврата УЗКОГО гарда.
@pytest.mark.parametrize(
    "leak",
    [
        "Я работаю на диффузионной модели Mercury, быстрее автогрессивных.",
        "Это GPT-4 от OpenAI.",
        "Под капотом у меня MiMo.",
        "Я построена на Gemini.",
    ],
)
def test_guard_disabled_postformat_does_not_redact(leak):
    """#216 отключён: _postformat больше НЕ подменяет ответ при упоминании бренда."""
    out = rl._postformat(leak)
    assert rl._IDENTITY_SAFE not in out, f"_postformat всё ещё редактирует: {out!r}"


def test_real_news_answer_with_openai_survives():
    """Регресс по живому инциденту (Andrey): ответ про AI-новости с «(экс-OpenAI)»
    НЕ должен схлопываться в identity-заглушку — бренд в контенте легитимен."""
    answer = ("Вот что посоветую: The Verge / AI, TechCrunch. Рассылки: "
              "Import AI — автор Jack Clark (экс-OpenAI), экспертный взгляд.")
    out = rl._postformat(answer)
    assert rl._IDENTITY_SAFE not in out, f"ответ снесло гардом: {out!r}"
    assert "OpenAI" in out  # бренд в контенте сохранён


@pytest.mark.parametrize(
    "ok",
    [
        "Готово, записала напоминание на завтра в 10:00.",
        "Вот твой список покупок:\n— молоко\n— хлеб",
        "Столица Австралии — Канберра.",
        "Сегодня в Москве облачно, к вечеру дождь.",
    ],
)
def test_normal_reply_passes(ok):
    out = rl._postformat(ok)
    assert rl._IDENTITY_SAFE not in out, f"ложное срабатывание на: {ok!r}"


def test_redact_identity_direct():
    assert rl._redact_identity("обычный текст про молоко") == "обычный текст про молоко"
    assert rl._redact_identity("работаю на Mercury") == rl._IDENTITY_SAFE
    assert rl._redact_identity("") == ""
