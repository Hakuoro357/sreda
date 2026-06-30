# -*- coding: utf-8 -*-
"""#3 — chat/fact-ветка (#197) на gemini-2.5-flash-lite через google-vertex/eu.

deepseek был медленным (4-11с/ход — жалоба реального юзера). gemini-flash-lite ≈0.7с.
Пин vertex/eu — data-residency. Слаг проверен вживую (713мс, без ошибки)."""
from __future__ import annotations

import sreda.services.llm as L


def test_gemini_flash_lite_model_mapping():
    assert L._OPENROUTER_MODEL_BY_PROVIDER["openrouter-gemini-2.5-flash-lite"] == (
        "google/gemini-2.5-flash-lite"
    )


def test_gemini_flash_lite_routes_vertex_eu():
    eb = L._OPENROUTER_EXTRA_BODY_BY_PROVIDER["openrouter-gemini-2.5-flash-lite"]
    prov = eb["provider"]
    assert prov["only"] == ["google-vertex/eu"], prov
    # data-residency: НЕ уходить на другой провайдер/регион при недоступности vertex/eu
    assert prov["allow_fallbacks"] is False
