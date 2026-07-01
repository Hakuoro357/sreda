# -*- coding: utf-8 -*-
"""gemini-2.5-flash-lite: маппинг модели + отсутствие пина на провайдера.

Провайдер используется пересказчиком #232 (``_SUMMARY_PROVIDER``) и легаси chat/fact #197.
Пин ``google-vertex/eu`` снят 2026-07-01 (Борис): EU-Vertex (единственный EU-регион у модели)
деградировал → 429 «rate-limited upstream» (тот же корень, что #257); ``only`` +
``allow_fallbacks=False`` блокировал уход на живой эндпоинт → пересказчик падал. Трейд-оф:
data-residency НЕ гарантирован (residency-сохраняющая альтернатива — BYOK Vertex-EU ключ)."""
from __future__ import annotations

import sreda.services.llm as L


def test_gemini_flash_lite_model_mapping():
    assert L._OPENROUTER_MODEL_BY_PROVIDER["openrouter-gemini-2.5-flash-lite"] == (
        "google/gemini-2.5-flash-lite"
    )


def test_gemini_flash_lite_no_provider_pin():
    # Пин снят 2026-07-01: без записи в extra_body OpenRouter роутит на здоровый провайдер
    # (EU-эндпоинт деградировал → 429). Ре-добавление пина без BYOK вернёт отказы пересказчика.
    assert "openrouter-gemini-2.5-flash-lite" not in L._OPENROUTER_EXTRA_BODY_BY_PROVIDER
