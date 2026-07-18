"""LlmCaller — transparent-passthrough seam for the chat LLM invoke kernel.

This module extracts the primary→fallback invoke kernel that previously lived
inline inside ``_run_legacy_react_loop`` in ``handlers.py``.  The extracted
behaviour is **byte-for-byte identical** to the original:

- per-call timeout via ``ainvoke_with_streaming_timeout``
- primary → fallback switching on any ``(LLMCallTimeout, Exception)``
- admin alerts (P0 when no fallback, P1 when fallback engaged), each
  wrapped in their own try/except so alert failures never crash the turn
- ``trace_meta["fallback"]`` and ``trace_meta["primary_exc"]`` mutations
- persist_request (primary + fallback), persist_response, persist_error
  callbacks are injected by the caller — no coupling to the envelope logic

Future extension point (PR-2b): reservation / deadline awareness.
``ainvoke_with_fallback`` accepts a ``reservation=None`` no-op parameter
so PR-2b can add logic there without touching the call-sites.  Do NOT
implement reservation logic here yet.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sreda.services.llm import LLMCallTimeout, ainvoke_with_streaming_timeout


class LlmCaller:
    """Per-iteration config carrier + invoke kernel.

    Construct once per loop iteration: ``per_call_timeout`` is re-read from
    settings each iteration in ``_run_legacy_react_loop``, so hoisting the
    construction out of the loop would capture a stale timeout.  Call
    ``ainvoke_with_fallback`` once per construction.
    """

    def __init__(
        self,
        *,
        primary_llm: Any,
        fallback_llm: Any,  # may be None
        primary_provider: str | None,
        fallback_provider: str | None,
        per_call_timeout: float,
        tenant_id: str,
        feature_key: str | None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.primary_llm = primary_llm
        self.fallback_llm = fallback_llm
        self.primary_provider = primary_provider
        self.fallback_provider = fallback_provider
        self.per_call_timeout = per_call_timeout
        self.tenant_id = tenant_id
        self.feature_key = feature_key
        # Preserve the ORIGINAL logger identity (Codex A/B #5 R1 MAJOR): the
        # extracted kernel previously logged under ``sreda.runtime.handlers``,
        # and the prod log format includes ``%(name)s`` — emitting under
        # ``sreda.runtime.llm_caller`` would change log bytes and could break
        # name-based log filters/alerts. The legacy call-site passes its module
        # logger; PR-2b may pass a planner-specific logger.
        self._logger = logger or logging.getLogger("sreda.runtime.handlers")

    async def ainvoke_with_fallback(
        self,
        messages: list[Any],
        *,
        iter_n: int,
        trace_meta: dict,
        on_text_update: Any,  # callable or None
        persist_request: Any,  # async callable
        persist_response: Any,  # async callable
        persist_error: Any,  # async callable
        reservation: Any = None,  # no-op; PR-2b extension point
    ) -> Any:
        """Invoke primary LLM with fallback.  Returns ``ai_msg``.

        ``persist_request``, ``persist_response``, ``persist_error`` are
        async callables provided by the caller (closures from
        ``_run_legacy_react_loop``).  Their signatures match the originals:
        - ``persist_request(attempt, llm_obj, provider, iter_n, per_call_timeout)``
        - ``persist_response(attempt, ai_msg, latency_ms, iter_n)``
        - ``persist_error(attempt, exc, latency_ms, iter_n)``

        ``reservation`` is accepted but unused (no-op default); PR-2b will
        add deadline/budget-check logic here without changing the call-sites.
        """
        # Issue #68: persist phase=request envelope BEFORE ainvoke.
        # Strong semantics — crash-safe: row на диске до того как LLM
        # стартует. Если require_persist=true и persist degraded — raise
        # (RuntimeError caught в outer handler).
        await persist_request(
            "primary", self.primary_llm,
            self.primary_provider, iter_n,
            self.per_call_timeout,
        )
        _t_primary_started = time.monotonic()
        # Audit 2026-07-18 (runtime-core #6): если primary успел
        # отстримить часть текста в ack и затем упал по таймауту,
        # fallback с тем же ``on_text_update`` видимо «откатывает»
        # ack и пишет его заново с начала (другой моделью — текст
        # не совпадёт с частичным). Отслеживаем факт частичного
        # стрима; если он был — fallback идёт без streaming-callback,
        # ack остаётся на месте и финальный ответ придёт одним edit'ом.
        _primary_streamed_text = False

        def _track_primary_stream(text_so_far: str) -> None:
            nonlocal _primary_streamed_text
            if text_so_far:
                _primary_streamed_text = True
            on_text_update(text_so_far)

        try:
            ai_msg = await ainvoke_with_streaming_timeout(
                self.primary_llm,
                messages,
                timeout_seconds=self.per_call_timeout,
                on_text_update=(
                    _track_primary_stream if on_text_update is not None else None
                ),
                provider=self.primary_provider,
            )
            # Issue #68: fire-and-forget response envelope (primary OK).
            _lat_ms_primary = int((time.monotonic() - _t_primary_started) * 1000)
            await persist_response("primary", ai_msg, _lat_ms_primary, iter_n)
        except (LLMCallTimeout, Exception) as exc:  # noqa: BLE001
            # Issue #68: persist phase=error envelope (primary failed)
            # — fire-and-forget. Latency measured from request start.
            _lat_ms_primary_err = int((time.monotonic() - _t_primary_started) * 1000)
            await persist_error("primary", exc, _lat_ms_primary_err, iter_n)
            # Любая ошибка primary (timeout / 5xx / rate limit) →
            # пытаемся fallback если он есть.
            # R-28: alert admin для **любой** LLM error (Codex R1 M3 —
            # fallback may be None для some configs, alert before re-raise).
            exc_type = type(exc).__name__
            try:
                from sreda.services.admin_alerts import send_admin_alert  # noqa: PLC0415
                if self.fallback_llm is None:
                    # No fallback configured — error propagates to caller.
                    # P0 severity: данный turn полностью failed, user видит error.
                    send_admin_alert(
                        severity="P0",
                        title=f"LLM primary failed, NO fallback: {exc_type}",
                        body=(
                            f"tenant: {self.tenant_id}\n"
                            f"feature: {self.feature_key}\n"
                            f"iter: {iter_n}\n"
                            f"primary_exc: {exc_type}\n"
                            f"reason: {str(exc)[:300]}\n"
                            f"impact: turn re-raised без fallback — user sees error"
                        ),
                        dedupe_key=f"llm_primary_no_fallback:{exc_type}:{self.feature_key}",
                    )
                else:
                    # Fallback engaged — P1, degraded but turn completes.
                    send_admin_alert(
                        severity="P1",
                        title=f"LLM fallback engaged: {exc_type}",
                        body=(
                            f"tenant: {self.tenant_id}\n"
                            f"feature: {self.feature_key}\n"
                            f"iter: {iter_n}\n"
                            f"primary_exc: {exc_type}\n"
                            f"reason: {str(exc)[:300]}"
                        ),
                        dedupe_key=f"llm_fallback:{exc_type}:{self.feature_key}",
                    )
            except Exception:  # noqa: BLE001 — alert must not crash turn
                self._logger.exception("R-28 admin_alert dispatch failed")
            if self.fallback_llm is None:
                raise
            self._logger.warning(
                "LLM_FALLBACK_ENGAGED tenant=%s feature=%s iter=%d "
                "primary_exc=%s reason=%s — switching to fallback",
                self.tenant_id, self.feature_key, iter_n,
                exc_type, str(exc)[:120],
            )
            trace_meta["fallback"] = True
            trace_meta["primary_exc"] = exc_type
            # Issue #68: persist phase=request envelope для fallback
            # — separate config (different provider/model/extra_body).
            await persist_request(
                "fallback", self.fallback_llm,
                self.fallback_provider, iter_n,
                self.per_call_timeout,
            )
            _t_fallback_started = time.monotonic()
            try:
                ai_msg = await ainvoke_with_streaming_timeout(
                    self.fallback_llm,
                    messages,
                    timeout_seconds=self.per_call_timeout,
                    # primary уже показал частичный текст в ack →
                    # не ре-стримим поверх (см. комментарий выше).
                    on_text_update=(
                        None if _primary_streamed_text else on_text_update
                    ),
                    provider=self.fallback_provider,
                )
                _lat_ms_fallback = int((time.monotonic() - _t_fallback_started) * 1000)
                await persist_response("fallback", ai_msg, _lat_ms_fallback, iter_n)
            except Exception as fb_exc:  # noqa: BLE001
                _lat_ms_fb_err = int((time.monotonic() - _t_fallback_started) * 1000)
                await persist_error("fallback", fb_exc, _lat_ms_fb_err, iter_n)
                raise
        return ai_msg
