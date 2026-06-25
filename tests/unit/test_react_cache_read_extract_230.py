# -*- coding: utf-8 -*-
"""#230 Срез 0a: извлечение cache_read из usage_metadata (наблюдаемость prompt-кеша).

Контракт (R4-ревью): бюджет по-прежнему получает (input, output) int через _extract_usage;
cache_read извлекается ОТДЕЛЬНО (_extract_cache_read) и идёт только в наблюдательный трейс #192,
НЕ в денежный учёт. Нормализованный путь как у llm_trace.extract_usage:
usage_metadata.input_token_details.cache_read (OpenAI-style), fallback .cached."""
from __future__ import annotations

import sreda.runtime.react_loop as rl


class _Resp:
    def __init__(self, usage):
        self.usage_metadata = usage


def test_cache_read_openai_style():
    r = _Resp({"input_tokens": 1000, "output_tokens": 50,
               "input_token_details": {"cache_read": 800}})
    assert rl._extract_cache_read(r) == 800


def test_cache_read_cached_fallback():
    r = _Resp({"input_tokens": 1000, "output_tokens": 50,
               "input_token_details": {"cached": 700}})
    assert rl._extract_cache_read(r) == 700


def test_cache_read_absent_is_zero():
    r = _Resp({"input_tokens": 1000, "output_tokens": 50})
    assert rl._extract_cache_read(r) == 0


def test_cache_read_no_usage_is_zero():
    assert rl._extract_cache_read(_Resp(None)) == 0
    assert rl._extract_cache_read(object()) == 0


def test_cache_read_negative_clamped():
    r = _Resp({"input_token_details": {"cache_read": -5}})
    assert rl._extract_cache_read(r) == 0


def test_extract_usage_budget_contract_unchanged():
    # Регресс: денежный контракт (input, output) tuple НЕ изменился добавлением cache_read.
    r = _Resp({"input_tokens": 1000, "output_tokens": 50,
               "input_token_details": {"cache_read": 800}})
    assert rl._extract_usage(r) == (1000, 50)
