# -*- coding: utf-8 -*-
"""#216 — гард личности: ответ не должен раскрывать базовую модель/провайдера."""
from __future__ import annotations

import pytest

import sreda.runtime.react_loop as rl


@pytest.mark.parametrize(
    "leak",
    [
        "Меня создала команда Inception Labs — AI-компания из Кремниевой долины.",
        "Я работаю на диффузионной модели Mercury, быстрее автогрессивных.",
        "Под капотом у меня MiMo.",
        "Я построена на Gemini.",
        "Это GPT-4 от OpenAI.",
        "My roots are Inception's diffusion-based language model.",
        "Я просто GPT, разговорный ИИ.",                 # голый GPT (Codex R1)
        "Работаю на Меркьюри.",                          # транслит Mercury
        "Я авторегрессивная модель.",                    # корректное написание
        "Это GPT‑4 (юникод-дефис) от OpenAI.",           # юникод-дефис
        "Под капотом Инцепшн.",                          # транслит Inception
    ],
)
def test_provider_leak_replaced(leak):
    out = rl._postformat(leak)
    assert out == rl._IDENTITY_SAFE, f"утечку не подменили: {out!r}"
    assert "@BorisPechorin" in out


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
