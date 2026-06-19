"""Chat LLM service (Phase 3).

Thin wrapper around LangChain's ``ChatOpenAI`` pointed at an
OpenAI-compatible endpoint (primary: MiMo-V2-Pro). Returns ``None``
when no API key is configured — callers must tolerate "LLM disabled"
gracefully for dev/test scenarios where we don't want to call live
providers.

Why LangChain: tool-binding machinery (``.bind_tools([...])``) and
structured-output support are the two levers Phase 3e needs. Writing
these from scratch against raw ``httpx`` would be a week of work per
provider. Pinning to ``langchain-openai`` also means swapping
providers later (if MiMo rate-limits us) is a one-line change in
settings — no code refactor.

Parallel tool-calls: verified 2026-04-22 that MiMo-V2-Pro emits
multiple ``tool_calls`` in a single assistant message when the
prompt invites it (e.g. "что в списке И что в меню"). The
``execute_conversation_chat`` loop handles the list correctly —
saves ~1 LLM round-trip (~3-5s) on multi-read turns. No flag to
flip; behaviour is default.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from typing import Any, Callable, Iterator

from langchain_core.messages.utils import message_chunk_to_message
from langchain_openai import ChatOpenAI

from sreda.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-call timeout enforcement (2026-04-28)
# ---------------------------------------------------------------------------
#
# `ChatOpenAI(timeout=60)` сам по себе ненадёжен: на проде наблюдали
# ответ от MiMo через 131 секунду без TimeoutError'а (видимо streaming
# resetит per-chunk timer, либо langchain не передаёт timeout в httpx).
# Без явного timeout fallback chain `.with_fallbacks([grok])` НЕ
# срабатывает — нет исключения чтобы поймать.
#
# Решение: внешний timeout через ThreadPoolExecutor. Если invoke не
# завершился за N секунд — кидаем TimeoutError, который ловится
# RunnableWithFallbacks → переключается на fallback провайдер.
#
# Side effect: hung thread продолжает работать (httpx connection кладётся
# в pool / GC). На наших объёмах (10 concurrent users) это допустимо.
# ThreadPoolExecutor создаётся per-call намеренно — иначе hung threads
# накапливались бы в shared pool.

_PER_CALL_TIMEOUT_DEFAULT = 60.0


class LLMCallTimeout(TimeoutError):
    """Raised by ``invoke_with_per_call_timeout`` when LLM exceeds wall-time.

    Inherits TimeoutError → langchain RunnableWithFallbacks ловит как
    Exception и переключается на fallback провайдер.
    """


def invoke_with_per_call_timeout(
    runnable: Any,
    messages: list,
    *,
    timeout_seconds: float = _PER_CALL_TIMEOUT_DEFAULT,
) -> Any:
    """Invoke `runnable.invoke(messages)` с жёстким wall-clock timeout.

    Args:
        runnable: LangChain Runnable (typically `llm.bind_tools(...)`).
        messages: список Message objects для invoke.
        timeout_seconds: лимит. Default 60s (= mimo_request_timeout_seconds).

    Returns:
        Результат runnable.invoke(messages) (обычно AIMessage).

    Raises:
        LLMCallTimeout: если timeout превышен. Поднимает TimeoutError-
            совместимый exception, который ловится `with_fallbacks` chain.
        Любые другие исключения от runnable пробрасываются как есть.
    """
    # Per-call executor — изолируем hung thread от shared pool.
    # ВАЖНО: НЕ используем `with` block — exit() вызывает shutdown(wait=True)
    # и ждёт пока медленный thread завершится, перечёркивая весь смысл
    # timeout'а. Делаем shutdown(wait=False, cancel_futures=True) вручную.
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="llm-invoke"
    )
    future = executor.submit(runnable.invoke, messages)
    try:
        result = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        # Hung thread остаётся в executor'е, его нельзя убить из Python.
        # shutdown(wait=False) не блокирует наш возврат. cancel_futures=True
        # cancel'ит pending tasks (хотя у нас одна running, она не cancel'ится).
        # Главное: текущий turn НЕ блокируется в ожидании MiMo.
        executor.shutdown(wait=False, cancel_futures=True)
        raise LLMCallTimeout(
            f"LLM invoke exceeded {timeout_seconds}s wall time"
        ) from exc
    except Exception:
        # Любые другие исключения (provider errors, и т.п.) — пробрасываем.
        # Executor cleanup идёт в обычном порядке после раскрутки.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        # Успех. Cleanup non-blocking.
        executor.shutdown(wait=False, cancel_futures=True)
        return result


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _message_reasoning_content(message: Any) -> Any:
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    return additional_kwargs.get("reasoning_content")


def _message_has_tool_calls(message: Any) -> bool:
    return bool(
        getattr(message, "tool_calls", None)
        or getattr(message, "tool_call_chunks", None)
    )


async def ainvoke_with_streaming_timeout(
    runnable: Any,
    messages: list,
    *,
    timeout_seconds: float = _PER_CALL_TIMEOUT_DEFAULT,
    on_text_update: Callable[[str], None] | None = None,
) -> Any:
    """Async stream/invoke wrapper with the same wall-clock timeout contract.

    Uses ``runnable.stream`` when a progress callback is provided and the
    runnable supports streaming. Otherwise falls back to the existing
    ``invoke_with_per_call_timeout`` path in a worker thread so the asyncio
    event loop remains free for ack-message edits.
    """
    if on_text_update is None or not hasattr(runnable, "stream"):
        return await asyncio.to_thread(
            invoke_with_per_call_timeout,
            runnable,
            messages,
            timeout_seconds=timeout_seconds,
        )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="llm-stream"
    )

    def _worker() -> None:
        try:
            for chunk in runnable.stream(messages):
                loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
        except BaseException as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

    executor.submit(_worker)
    deadline = loop.time() + timeout_seconds
    combined: Any | None = None
    text_so_far = ""
    saw_tool_chunk = False

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise LLMCallTimeout(
                    f"LLM stream exceeded {timeout_seconds}s wall time"
                )
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise LLMCallTimeout(
                    f"LLM stream exceeded {timeout_seconds}s wall time"
                ) from exc

            if kind == "error":
                raise payload
            if kind == "done":
                break

            chunk = payload
            combined = chunk if combined is None else combined + chunk
            if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
                saw_tool_chunk = True
                continue
            text_piece = _chunk_text(chunk)
            if text_piece and not saw_tool_chunk:
                text_so_far += text_piece
                on_text_update(text_so_far)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if combined is None:
        return await asyncio.to_thread(
            invoke_with_per_call_timeout,
            runnable,
            messages,
            timeout_seconds=timeout_seconds,
        )
    result = message_chunk_to_message(combined)
    if (
        _message_has_tool_calls(result)
        and not _message_reasoning_content(result)
        and hasattr(runnable, "invoke")
    ):
        return await asyncio.to_thread(
            invoke_with_per_call_timeout,
            runnable,
            messages,
            timeout_seconds=timeout_seconds,
        )
    if text_so_far and not _message_has_tool_calls(result):
        # The ack UX streams ``text_so_far`` to the user as the visible
        # answer. Keep the final message on the same source of truth so
        # the last edit cannot repaint it with a provider/LangChain
        # normalized variant.
        try:
            result.content = text_so_far
        except Exception:  # noqa: BLE001
            if hasattr(result, "model_copy"):
                result = result.model_copy(update={"content": text_so_far})
            elif hasattr(result, "copy"):
                result = result.copy(update={"content": text_so_far})
    return result


# Models trained on ReAct-style tool-calling data (Gemma-4 family,
# some Qwen variants, early DeepSeek R1 forks) occasionally prepend
# reasoning-trace markers to their final user-visible text. Verified
# 2026-04-22 on google/gemma-4-26b-a4b-it: every tool-calling reply
# came back as ``thought\n<real answer>``. System prompt rules can't
# suppress it — the training signal overrides user instructions.
# Fixing it at the boundary (once, where we extract the reply text)
# keeps the rest of the code provider-agnostic.
_REASONING_PREFIXES = (
    "thought", "thinking", "reasoning", "analysis", "internal",
    "reflect", "reflection",
)
# Matches a leading ReAct marker on its own line or followed by ":".
# Case-insensitive. Example hits: ``thought\n``, ``Thinking: ``,
# ``REASONING\n\n``. The whole marker + following whitespace is
# stripped; the actual answer stays intact.
_REASONING_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(_REASONING_PREFIXES) + r")\s*[:\n]\s*",
    re.IGNORECASE,
)


# Full tool-call signatures occasionally leak into the text channel
# on Gemma-4 / Grok (reproduced 2026-04-22: ``:://plan_week_menu(
# week_start='2026-04-26', days=[{...}])``, ``search_recipes(query=
# 'X')\nsave_recipe(...)``, etc). The proper tool_calls JSON DID
# fire in earlier iterations — this is the "wrap-up" message
# re-narrating the actions as raw syntax, which looks like a bug to
# the user.
#
# Our housewife tool names are finite and have a distinctive
# ``<snake_case>(<args>)`` shape — enumerating them lets us scrub
# anywhere in the text, not just at the start. This fixes the prod
# case where the model prepended garbage like ``://`` before the
# tool-call and our anchored regex skipped the match.
_KNOWN_TOOL_NAMES = (
    # Memory
    "save_core_fact", "save_episode", "recall_memory",
    # Web
    "web_search", "fetch_url", "log_unsupported_request",
    # Reminders
    "schedule_reminder", "cancel_reminder", "list_reminders",
    # Housewife shopping
    "list_shopping", "add_shopping_items", "remove_shopping_items",
    "mark_shopping_bought", "clear_bought_shopping",
    "update_shopping_item", "update_shopping_items_category",
    # Housewife recipes
    "search_recipes", "get_recipe", "save_recipe", "save_recipes_batch",
    "delete_recipe",
    # Housewife menu
    "plan_week_menu", "list_menu", "update_menu_item",
    "generate_shopping_from_menu", "clear_menu",
    # Housewife family
    "add_family_members", "remove_family_member", "update_family_member",
    "list_family_members",
    # Task scheduler («Расписание»)
    "add_task", "list_tasks", "update_task",
    "complete_task", "uncomplete_task", "cancel_task", "delete_task",
    "attach_reminder", "detach_reminder",
    # Onboarding
    "onboarding_answered", "onboarding_deferred", "onboarding_complete",
)
_TOOL_NAME_ALT = "|".join(re.escape(name) for name in _KNOWN_TOOL_NAMES)
# Match a known tool name + its paren-wrapped args, optionally
# preceded by URL-like junk (``://``, ``http://``, etc) — greedy on
# the args body so multi-line signatures fold together. Non-anchored
# so it strips occurrences anywhere in the reply. DOTALL so a nested
# newline inside args[] still gets swallowed.
_TOOL_CALL_ANYWHERE_RE = re.compile(
    r"(?:https?://|://)?(?:" + _TOOL_NAME_ALT + r")\([^)]*\)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_tool_call_syntax(text: str) -> str:
    """Remove any recognisable tool-call signature from anywhere in
    the text. Only matches against the known tool name list, so
    prose mentioning ``add`` or ``save`` in a sentence isn't affected
    — the ``(`` after a full tool identifier is the discriminator."""
    cleaned = _TOOL_CALL_ANYWHERE_RE.sub("", text)
    # Collapse whitespace runs left behind by the removal so the
    # remaining prose reads naturally instead of with stray blank
    # lines where the signatures used to be.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# Internal DB identifiers that occasionally leak into user-facing
# text — observed 2026-04-22 on x-ai/grok-4.1-fast dumping
# ``[rec_f5197...]`` after every meal line. Users don't need these
# (they're FK keys for tool calls) and seeing them reads as a
# rendering bug. Strip any occurrence of ``<space>[rec_hex]`` /
# ``<space>[menu_hex]`` / ``<space>[sh_hex]`` — with or without the
# brackets — from the middle or end of any line. Prefix and core
# pattern list kept narrow so a legitimate mention of e.g. 'recipe'
# in prose isn't touched.
_INTERNAL_ID_RE = re.compile(
    r"\s*[\[\(]?"
    r"(?:rec|menu|mpi|sh|ring|ob|run|thread|job|evt)_[a-f0-9]{8,}"
    r"[\]\)]?",
)


def strip_reasoning_prefix(text: str) -> str:
    """Remove leading ReAct-style meta from an LLM reply — both the
    short ``thought\\n`` marker (Gemma-4 default) and fully-expanded
    tool-call syntax that sometimes leaks into the text channel when
    the model re-narrates its actions instead of just writing a
    human reply. Also scrubs internal DB identifiers
    (``rec_...``, ``menu_...``, ``sh_...``) that some models echo
    next to items as a pseudo-helpful reference.

    Returns ``text`` unchanged if nothing matches. Idempotent —
    applying twice equals once.
    """
    if not text:
        return text
    match = _REASONING_PREFIX_RE.match(text)
    if match:
        text = text[match.end():]
    # Leaked tool-call syntax can appear anywhere in the reply —
    # sometimes at the start (Gemma opens with ``search_recipes(...)``),
    # sometimes mid-message after a garbage prefix like ``://``
    # (observed 2026-04-22 on a Sunday-menu turn). Scan the whole
    # string for known tool names + paren args.
    text = _strip_tool_call_syntax(text)
    # Internal-ID scrubber runs last so the cleanup is visible in the
    # final reply regardless of which meta layer was also stripped.
    text = _INTERNAL_ID_RE.sub("", text)
    return text


# Write-tools whose invocation we expect when the LLM claims a stable
# side-effect ("сохранил рецепт", "добавила в список", "создал меню").
# Used by ``detect_unbacked_claim`` to spot Gemma-4 hallucinations
# where the model narrates an action without actually calling the
# corresponding tool. Keep ordered as-registered — dispatch is by
# membership only.
_WRITE_TOOL_NAMES = frozenset({
    "save_core_fact", "save_episode",
    "save_recipe", "save_recipes_batch", "delete_recipe",
    "add_shopping_items", "remove_shopping_items", "mark_shopping_bought",
    "update_shopping_item", "update_shopping_items_category",
    "plan_week_menu", "update_menu_item", "generate_shopping_from_menu",
    "add_family_members", "remove_family_member", "update_family_member",
    "schedule_reminder", "cancel_reminder",
    # Task scheduler write-tools (ack claims like "поставила задачу")
    "add_task", "update_task", "complete_task", "uncomplete_task",
    "cancel_task", "delete_task", "attach_reminder", "detach_reminder",
})

# Verb+object pairs that indicate the model claims a side-effect.
# Conservative by design — must match a verb AND an object noun in
# the same sentence to fire. Too many false positives would cost us a
# wasted LLM iteration on every benign "сохраню это на будущее".
_CLAIM_VERBS = ("сохранил", "сохранила", "сохранено",
                "добавил", "добавила", "добавлено",
                "создал", "создала", "создано",
                "записал", "записала", "записано",
                "удалил", "удалила", "удалено",
                "поставил напомин", "поставила напомин",
                "запланировал", "запланировала",
                # 2026-05-22 (задача #59): синхронизация с расширенным
                # ban-list в _TOOL_DISCIPLINE_ADDENDUM (handlers.py L1598).
                # Промпт запрещает эти глаголы LLM-у без tool_call —
                # детектор должен ловить если LLM игнорирует инструкцию.
                "внесла", "внесло", "внесли", "внёс", "внес", "внесён",
                "зафиксировал", "зафиксировала",
                "отметил", "отметила",
                "обновил", "обновила",
                # 2026-04-29: incident user_tg_352612382. LLM сказал
                # «Готово! ⏰ Каждый день в 9:00 утра будет напоминание»
                # без вызова schedule_reminder. Прежние паттерны были
                # только activе («поставил/добавил») — passive future
                # forms ("будет напомин") и общие affirmation
                # («готово/готова») проходили мимо детектора.
                "готово", "готова",
                "будет напомин",
                # 2026-04-29 round 2: completion-markers без явного
                # глагола. LLM может ответить «✅ В списке покупок»
                # / «Сделано! Список обновлён» без vocabulary-verb'а.
                # Эти markers fire с object'ом рядом (тот же contract:
                # claim of action + reference to domain object).
                "сделано", "выполнено", "установлено", "записано",
                "зафиксировано", "отмечено", "учтено",
                "✅", "☑️", "☑")


# 2026-05-29 (incident user_tg_755682022, vex-assistant#79): READ-tools
# that render a checklist emit completion markers (☑/☐/✗ from
# `show_checklist` in housewife_chat_tools.py, plus the word «выполнено»
# as a section header). Those markers double as claim-verbs (_CLAIM_VERBS)
# whose checklist-category expected_tools are WRITE-only — so every display
# of a checklist with done items false-fired detect_unbacked_claim, and the
# handler then REPLACED the correct reply with a safe-ack (handlers.py:3451).
#
# Fix is category-scoped, NOT a blanket text strip (Codex r1 MAJOR #1/#2):
# when a checklist DISPLAY tool was actually called AND the matched claim
# verb is a checklist STATE marker (not a write verb), the «checklist»
# category is also backed by that read tool. So:
#   • «☑ X» / «Выполнено: ☑ X» + show_checklist → backed (state marker).
#   • «Добавила пункт X» + only show_checklist → still fires (write verb,
#     read tool does not back a mutation claim).
#   • «✅ В список покупок: молоко» + show_checklist → still fires (object
#     is shopping, not checklist — display backing never applies).
_CHECKLIST_DISPLAY_TOOLS: frozenset[str] = frozenset({"show_checklist"})
# Subset of _CLAIM_VERBS that describe rendered checklist item STATE rather
# than a mutation. Only these get the read-tool backing for the «checklist»
# category. Deliberately narrow (Codex r2 high MAJOR/MEDIUM):
#   • «☑»/«☑️» — the exact done-glyph `show_checklist` renders.
#   • «выполнено» — the section header used to present done items.
# EXCLUDED on purpose:
#   • «✅» — a generic completion affirmation `show_checklist` never emits;
#     keeping it would mask a real write claim like «✅ В чек-лист: молоко».
#   • «отмечено» — reads as a passive write-ack («[я] отметила»), not a
#     tool-rendered state; not emitted by `show_checklist`.
#   • write verbs (добавил/удалил/создал/...) — mutations, never read-backed.
_CHECKLIST_STATE_MARKER_VERBS: frozenset[str] = frozenset({
    "☑️", "☑", "выполнено",
})


# 2026-05-11 (Codex+Xiaomi r1 на Nemotron incident): 1sg FUT/PRES
# claim-глаголы. На проде 21:23-21:24 Nemotron 3 turns подряд писал
# «Хорошо, поставлю напоминание», «ставлю напоминание на 09:50»
# без tool call'а — detector не сработал, ловил только past-tense.
#
# В отличие от past-tense ("поставил" = факт сделал), 1sg FUT/PRES
# часто появляется в QUESTION / OFFER формах: «Хочешь, поставлю
# напоминание?» — это offer/вопрос, не claim. Нужен context-check.
# Pattern fires только если предложение НЕ оканчивается на "?" в
# окне 80 chars вокруг verb (см. detect_unbacked_claim Pass 1c).
_CLAIM_VERBS_FUTURE: tuple[str, ...] = (
    "поставлю", "ставлю",
    "сохраню", "сохраняю",
    "добавлю", "добавляю",
    "создам", "создаю",
    "запишу", "записываю",
    "удалю", "удаляю",
    "запланирую", "планирую",
    "отмечу", "отмечаю",
    "обновлю", "обновляю",
    "напомню", "напоминаю",
)
_CLAIM_OBJECTS = ("рецепт", "в книг", "в список", "в покупк",
                  "в меню", "меню на", "напомина", "семь",
                  "в твою книг", "в твой список",
                  # Task scheduler claims: "поставила задачу",
                  # "добавила в расписание", "запланировала на".
                  "задач", "в расписан", "запланирова",
                  # 2026-04-28: checklist claims (incident tg_634496616).
                  # «Добавила ✅\n— ☐ Х» при tools=[] = галлюцинация.
                  # Маркер «☐»/«☑»/«☒»/«✗» в выводе = претензия на
                  # отображение чек-листа.
                  "чек-лист", "чек лист", "пункт",
                  "☐", "☑", "☒", "✗",
                  # 2026-05-08 incident (tenant_tg_755682022): LLM
                  # написала «Записала в план кроя на пятницу» когда
                  # был вызван add_shopping_items.
                  # 2026-05-29: «План кроя» — это чек-лист по имени,
                  # категория поправлена на "checklist" в
                  # _OBJECT_TO_CATEGORY (см. ниже). Швейный процесс
                  # «в крой» / « крой » остаётся None — tool'а нет.
                  # Codex r3 MAJOR #2: only specific phrases — bare
                  # "крой" matches "раскрой", "покрой" (часть слов
                  # «раскрой теста», «покрой одежды»).
                  "план кро", "в крой ", " крой ", "план на крой")


# 2026-05-08: object → tool-category mapping. Category=None означает
# что в системе НЕТ tool'а для этого claim type — любая такая claim
# = unbacked всегда. Это закрывает 2026-05-08 cutting plan bug.
#
# Substrings checked в порядке specificity (длинные первыми) — чтобы
# «в план кроя» не матчился просто на «в покупк» если оба в окне.
_OBJECT_TO_CATEGORY: tuple[tuple[str, str | None], ...] = (
    # «План кроя» — это чек-лист по имени (юзер хранит позиции
    # раскроя как пункты чек-листа). 2026-05-29 incident
    # tenant_tg_755682022: LLM вызвал add_checklist_items с
    # list_id_or_title="План кроя", получил ok, но safety_net
    # отверг это как unbacked (категория была None) → retry-нудж →
    # двойной add_checklist_items → дубль "Муслин зелёный широкий"
    # и в "План кроя", и в "Дела по ткани". Маппим в "checklist",
    # чтобы add_checklist_items / create_checklist принимались как
    # валидный backing. См. trace_73c9fe5786b04838 16:33 UTC.
    ("план кро", "checklist"),     # "план кроя", "план крой"
    ("план на крой", "checklist"),
    # Сам процесс кроя (швейная операция) — tool'а нет. Любая claim
    # «в крой ...» / « крой ...» остаётся unbacked.
    # ⚠ Codex r3 MAJOR #2: только specific фразы. Bare "крой"
    # ловит «раскрой», «покрой» (раскрой ткани, покрой одежды) —
    # false positives. Phrase-based matching tighter.
    ("в крой ", None),         # "в крой на пятницу"
    (" крой ", None),          # bare "крой" surrounded by spaces
    # Recipe
    ("рецепт", "recipe"),
    ("в книг", "recipe"),
    # Shopping
    ("в покупк", "shopping"),
    ("в список покуп", "shopping"),
    ("в твой список", "shopping"),
    ("в список", "shopping"),
    # Menu
    ("в меню", "menu"),
    ("меню на", "menu"),
    # Reminder
    ("напомина", "reminder"),
    # Family
    ("семь", "family"),
    # Task
    ("задач", "task"),
    ("в расписан", "task"),
    ("запланирова", "task"),
    # Checklist
    ("чек-лист", "checklist"),
    ("чек лист", "checklist"),
    ("пункт", "checklist"),
    ("☐", "checklist"),
    ("☑", "checklist"),
    ("☒", "checklist"),
    ("✗", "checklist"),
)


# 2026-05-08: tool-name → category. Используется в category-aware
# проверке detect_unbacked_claim.
_CATEGORY_TO_TOOLS: dict[str, frozenset[str]] = {
    "recipe": frozenset({
        "save_recipe", "save_recipes_batch", "delete_recipe",
    }),
    "shopping": frozenset({
        "add_shopping_items", "remove_shopping_items",
        "mark_shopping_bought", "update_shopping_item",
        "update_shopping_items_category", "clear_bought_shopping",
    }),
    "menu": frozenset({
        "plan_week_menu", "update_menu_item",
        "generate_shopping_from_menu", "clear_menu",
    }),
    "reminder": frozenset({
        "schedule_reminder", "cancel_reminder", "update_reminder",
        "attach_reminder", "detach_reminder",
    }),
    "family": frozenset({
        "add_family_members", "update_family_member",
        "remove_family_member",
    }),
    "task": frozenset({
        "add_task", "update_task", "complete_task",
        "uncomplete_task", "cancel_task", "delete_task",
    }),
    "checklist": frozenset({
        "create_checklist", "add_checklist_items",
        "move_task_to_checklist", "mark_checklist_item_done",
        "delete_checklist_item", "archive_checklist",
    }),
    "memory": frozenset({"save_core_fact", "save_episode"}),
}


# 2026-04-29: self-asserting verbs — verb itself implies a side-effect,
# не требует ближнего object'а. «Напомню тебе завтра в 9» сам по себе
# claim что reminder будет создан, без отдельного слова «напоминание».
# Held in separate set чтобы первое-pass проверка была быстрее (small
# set + early-return).
_SELF_CLAIMING_VERBS = frozenset({
    "напомню", "напоминаю", "напоминать буду", "буду напоминать",
})


# 2026-05-08 r2 (Boris feedback "только крой?"): self-asserting verbs
# для actions, для которых **NO tool exists**. Любое появление в тексте
# = unbacked (LLM не может реально выполнить эти действия). Phrase-based
# чтобы избежать false-positive на benign mentions:
#  - «оплатила счёт» specific → unbacked. «оплата» одна — не fire.
#  - «сшила» / «выкроила» specific 1st-person past → unbacked
#    (швея 755 говорит «я сшила» в дискуссии — false-fire risk
#    приемлем т.к. мягкий retry, не финальный block).
#  - «позвонила в»/«позвонил в» — completed call claim. «позвонить»
#    (infinitive) НЕ совпадает.
_SELF_CLAIMING_NO_TOOL: frozenset[str] = frozenset({
    # Sewing actions — power-user 755 risk
    "сшила", "сшил",
    "выкроила", "выкроил",
    "пошила", "пошил",
    # Payments
    "оплатила счёт", "оплатил счёт",
    "оплатила счет", "оплатил счет",
    "оплатила квитанц", "оплатил квитанц",
    "перевела деньги", "перевёл деньги",
    "перевел деньги",
    "перевела на счёт", "перевел на счет",
    # Booking / travel
    "заказала такси", "заказал такси",
    "вызвала такси", "вызвал такси",
    "забронировала", "забронировал",
    "записала к врачу", "записал к врачу",
    # Communication
    "отправила письмо", "отправил письмо",
    "отправила email", "отправил email",
    "отправила смс", "отправил смс",
    # Phone calls (completed action, not "напомни позвонить")
    "позвонила в ", "позвонил в ",
    "позвонила маме", "позвонил маме",
    # Image / file gen
    "сгенерировала картин", "сгенерировал картин",
    "сгенерировала изображ", "сгенерировал изображ",
    # Calendar
    "поставила в календар", "поставил в календар",
    "добавила в календар", "добавил в календар",
    "встречу в календар",
    # Government services
    "подала заявление", "подал заявление",
    "оформила документ", "оформил документ",
    "загрузила в госуслуг", "загрузил в госуслуг",
})


# 2026-05-11 (Codex+Xiaomi consensus on user_tg_755682022 incident):
# READ-side claim detection. Production turns 16:18-16:33 — модель
# зацитировала «у тебя в чек-листе X», «оттуда и взяла», прогноз
# погоды +14°C на 17 мая, детальный план кроя с размерами 220×240 —
# всё БЕЗ соответствующего read-tool в same turn (called_tools=[]).
#
# Контракт Pass 4 в detect_unbacked_claim:
#   verb match (read-citation phrase) + object match (domain noun)
#   + expected read-tool NOT in called_tools → unbacked.
#
# Verb list — specific citation phrases, не generic verbs. «есть»/
# «вижу» одни не fire'ят (слишком много benign occurrences). Phrase
# «у тебя в», «висят как», «оттуда» — typical reference-to-state.
_READ_CLAIM_VERBS: tuple[str, ...] = (
    "взяла из", "взял из", "взяла оттуда", "взял оттуда",
    "нашла в", "нашёл в", "нашел в",
    "прочитала", "прочитал",
    "вижу в", "вижу что",
    "есть в твоём", "есть в твоем", "есть в твоей",
    "висят в", "висят как", "висит как",
    "отмечен как", "отмечена как", "отмечены как",
    "помечен как", "помечена как", "помечены как",
    "по записям", "по памяти",
    "в моей памяти", "в твоей памяти",
    "у тебя в", "у меня записан",
    "я помню что", "помню что у тебя",
    "по плану", "из плана",
    "оттуда и взяла", "оттуда и взял", "оттуда взяла",
    "в твоём списке", "в твоем списке", "в твоей книг",
    "в нашей памяти", "в нашем списке",
)


# 2026-05-11: object → expected read-tool. Чек: если verb match +
# object match → category determines какой tool ДОЛЖЕН был быть
# вызван. Если ни один из expected tools НЕ в called_tools — unbacked.
#
# Substrings проверяются в порядке specificity (длинные первыми).
# Каждый entry — (substring, frozenset of expected read-tool names).
_READ_OBJECT_TO_TOOL: tuple[tuple[str, frozenset[str]], ...] = (
    # Cutting plan / checklists — stored as checklists or memory.
    # Все падежи «план кроя»: nom «план кро», instr «плану кро»,
    # gen «плана кро», loc «плане кро».
    ("план кро", frozenset({"list_checklists", "show_checklist", "recall_memory"})),
    ("плану кро", frozenset({"list_checklists", "show_checklist", "recall_memory"})),
    ("плана кро", frozenset({"list_checklists", "show_checklist", "recall_memory"})),
    ("плане кро", frozenset({"list_checklists", "show_checklist", "recall_memory"})),
    # Checklist. Codex r2 #2: bare «пункт» dropped — false-positives
    # on «пункт назначения», «сборный пункт», «пункт повестки» where
    # nothing checklist-related is claimed. Чек-лист/чеклист/чек лист
    # сами по себе достаточный discriminator.
    ("чек-лист", frozenset({"list_checklists", "show_checklist"})),
    ("чеклист", frozenset({"list_checklists", "show_checklist"})),
    ("чек лист", frozenset({"list_checklists", "show_checklist"})),
    # Memory
    ("памят", frozenset({"recall_memory"})),
    ("в записях", frozenset({"recall_memory"})),
    ("записан", frozenset({"recall_memory"})),
    # Shopping — «покуп» — discriminating marker (покрывает все
    # падежи: «покупки/покупок/покупкам/покупка»). Должен идти
    # ПЕРЕД generic «в списк», иначе generic match-нёт первым и
    # засчитает list_menu как достаточный backing для shopping
    # claim. (Codex+Xiaomi consensus on impl review 2026-05-11.)
    ("покуп", frozenset({"list_shopping"})),
    # Generic «в списк» — fallback для cases без «покуп» (мог быть
    # menu или checklist). Намеренно accepts list_shopping ИЛИ
    # list_menu — нет способа без context'а сказать который.
    ("в списк", frozenset({"list_shopping", "list_menu"})),
    # Menu
    ("в меню", frozenset({"list_menu"})),
    ("меню на", frozenset({"list_menu"})),
    # Recipes
    ("в книг", frozenset({"search_recipes", "list_recipes"})),
    ("рецепт", frozenset({"search_recipes", "list_recipes"})),
    # Tasks
    ("задач", frozenset({"list_tasks"})),
    ("в расписан", frozenset({"list_tasks"})),
    ("в календар", frozenset({"list_tasks"})),
    # Reminders
    ("напомина", frozenset({"list_reminders"})),
    # Weather (special — different verbs, see detect_unbacked_claim
    # weather-specific path in handlers.py _sanitize_chat_reply).
    ("погод", frozenset({"get_weather"})),
    ("температур", frozenset({"get_weather"})),
    ("прогноз", frozenset({"get_weather"})),
    # Family
    ("семь", frozenset({"recall_memory"})),
)


# 2026-05-11 (Codex+Xiaomi r2 impl review): question-skip heuristic
# для Pass 4 read-side detector. Если "?" появляется в окне N chars
# ПОСЛЕ verb match — считаем форму вопросительной (бот спрашивает
# юзера), не lying citation, skip.
#
# Tradeoffs:
#   - 30 chars: пропускает legitimate cite с follow-up «...— хочешь?»
#     дальше 30 chars от verb. Tight = false-negative risk (claims
#     прокатывают как questions).
#   - 40 chars (current): balance. Production case "У тебя в списке
#     покупок есть мясо?" (34 chars) → skip; "Нашла в книге рецептов
#     X и Y — хочешь?" ("?" at 63 chars) → don't skip → fire correctly.
#   - 80+ chars: catches almost any "?" в reply — true-positive
#     citations прокатывают если есть следующее любой вопрос.
_READ_QUESTION_SKIP_WINDOW = 40


# 2026-05-12 (R-12 sreda#26): cap object-search window at sentence
# boundary. Production incident 2026-05-11 17:41 (user_max_40921122):
# bot reply «Записала на завтра в 13:00 — тренировка с гирей 💪\n\nНужно
# напоминание перед тренировкой?» — verb «записала» + object «напомина»
# из ВТОРОГО предложения попали в одно 160-char window → Pass 2 решил
# что claim про reminder, expected schedule_reminder в called_tools,
# не нашёл → false positive → safety_net заменил legitimate reply на
# safe_ack. Юзер потерял follow-up offer-question.
#
# Fix: search для object idёт только до конца текущего предложения от
# verb position (cut на `.`, `!`, `?`, `\n\n`). Если sentence boundary
# не найдена в +120 chars, оставляем cap +120.
# Boundaries — only `?` and `\n\n` (R-12 r2 после Codex review).
#
# `!` excluded — в RU «Готова! Запись добавлена.» = один claim
# (expressive emphasis, не sentence break).
#
# `.` excluded — Codex MAJOR #3, #4 r2:
#   1. «Готово. Список покупок обновлён.» without shopping tool —
#      раньше fired (legit completion-marker + cross-sentence claim),
#      теперь missed бы.
#   2. Точка в датах/сокращениях/URL («12.05», «т.к.», «и т.д.»,
#      «cloud.google.com») fragmentировала бы window.
#
# Что остаётся:
#   `?` — definite claim end. После него идёт offer/question или
#     новый topic — claim уже cёnut.
#   `\n\n` — paragraph break. Boris's prod incident 09:41:44 имел
#     именно `\n\n` между claim и offer-question.
#
# Side effect: window больше не cuts на `.`, потому cross-verb cases
# (Codex MAJOR #1) разрешаются через ALL-objects iteration (см. Pass 2).
_SENTENCE_BOUNDARY_MARKERS: tuple[str, ...] = ("?", "\n\n")


def _intra_sentence_window(
    low: str, verb_idx: int, verb_len: int,
    *, max_after: int = 120, pre_chars: int = 40,
) -> tuple[str, int]:
    """Return text window covering текущее предложение verb'а.

    Pre-verb: ищет latest sentence boundary в [verb_idx - pre_chars, verb_idx).
    Если найден — pre_start = после boundary. Иначе verb_idx - pre_chars.
    Post-verb: ищет earliest sentence boundary в [verb_idx + verb_len,
    verb_idx + verb_len + max_after). Если найден — end = после boundary.
    Иначе fallback на полный max_after.

    Защита от cross-sentence spillover: «Сохранила рецепт. Поставила
    напоминание.» с verb «поставила напомин» — pre-clip отрезает первое
    предложение, post-clip останавливает на «завтра.» → window содержит
    только claim про reminder. Если recipe-tool вызван а reminder нет,
    Pass 2 правильно fire'ит.
    """
    # Pre-verb: scan backwards для sentence boundary.
    pre_search_start = max(0, verb_idx - pre_chars)
    pre_text = low[pre_search_start:verb_idx]
    pre_start = pre_search_start  # default: no boundary
    for marker in _SENTENCE_BOUNDARY_MARKERS:
        idx = pre_text.rfind(marker)
        if idx >= 0:
            candidate = pre_search_start + idx + len(marker)
            if candidate > pre_start:
                pre_start = candidate

    # Post-verb: scan forward для sentence boundary.
    after_start = verb_idx + verb_len
    after_end_max = min(len(low), after_start + max_after)
    after_text = low[after_start:after_end_max]
    end_offset = len(after_text)  # default: no boundary found, use full max_after
    for marker in _SENTENCE_BOUNDARY_MARKERS:
        idx = after_text.find(marker)
        if idx >= 0:
            candidate = idx + len(marker)
            if candidate < end_offset:
                end_offset = candidate
    return low[pre_start: after_start + end_offset], pre_start


# R-12 r4 (Codex r3 MAJOR #1): offer-question detection per object.
# «Записала задачу, нужно напоминание?» с add_task — reminder это
# offer-question (modal «нужно» before object + `?` after), не claim.
# Plain `?` window skip (R-12 r2) был removed в r3 потому что past-
# tense Pass 2 не должен skip claims «задачу, ок?». Modal-based check
# distinguishes offer-question form от confirmation tail.
#
# Modal triggers: substrings проверяются в pre-object окне (30 chars,
# wider после Codex r4 / Xiaomi r4 «Хочешь, чтобы я добавил X?» case).
# При match + `?` в rest of window (anywhere после object up to
# sentence boundary which ends window) → offer-question, skip.
#
# Codex r4 MAJOR #1: window IS already bounded by intra-sentence cut
# (`?` or `\n\n`), так что unbounded search after object безопасен.
# Раньше 15c after окно miss'ило «нужно напоминание перед тренировкой?»
# где `?` 35+ chars after object stem «напомина».
_OFFER_QUESTION_BEFORE_CHARS = 30
_OFFER_QUESTION_MODAL_TRIGGERS: tuple[str, ...] = (
    "нужн",       # нужно/нужен/нужна/нужны
    "хочеш",      # хочешь
    "хотите",     # хотите
    "может",      # может / может быть (no trailing space — handles «может,» too)
    "можно",      # можно / можно ли (no trailing space)
    "стоит ли",   # стоит ли
    "не против",  # не против ли
    "хорошо ли",  # хорошо ли
    "будешь",     # будешь?
    "давай",      # давай / давайте (R-12 r5, Codex r4 MAJOR #2)
    "можем",      # можем / можем ли (R-12 r5, Codex r4 MAJOR #2)
    "могу",       # могу / могу ли (R-12 r6, Codex r5 MAJOR — «могу поставить X?»)
)


def _is_offer_question_for_object(
    window: str, obj_idx: int, obj_len: int,
) -> bool:
    """True если object'у предшествует modal trigger (within
    _OFFER_QUESTION_BEFORE_CHARS) И в REST of window после object
    есть `?`. Window уже sentence-bounded (intra-sentence cut на `?`
    или `\\n\\n`), так что unbounded post-search безопасен.
    """
    pre_start = max(0, obj_idx - _OFFER_QUESTION_BEFORE_CHARS)
    pre_text = window[pre_start:obj_idx]
    has_modal = any(m in pre_text for m in _OFFER_QUESTION_MODAL_TRIGGERS)
    if not has_modal:
        return False
    # `?` в остатке window (window сам ограничен sentence boundary).
    return "?" in window[obj_idx + obj_len:]


# R-12 r3 (Codex r2 MAJOR #1): proximity threshold для iterate-all-objects.
# Objects within этого расстояния от verb end рассматриваются как
# claim. Beyond — как mention/offer, ignore. Балансирует:
#   - «Записала задачу и напоминание» (dist 1, 12) — оба claim ✓
#   - «Сохранила рецепт — потом можно составить меню!» (dist 1, 40+) —
#     рецепт claim, меню mention ✗ не fire
_OBJECT_PROXIMITY_CLAIM_CHARS = 30


def _iter_claim_objects_in_window(
    window: str,
) -> "Iterator[tuple[str, str | None | bool, int]]":
    """Yield ALL matching (obj, category, idx) pairs в window
    (sorted по specificity = order в `_OBJECT_TO_CATEGORY`).

    R-12 r4 (Codex r3 MAJOR #2): yield ВСЕ occurrences каждого obj,
    не только первое. «Записала на завтра в 13:00 — тренировку, а
    также — задачу» — first «задач»-like at distance 50+ (mention),
    но если бы был второй ближе — раньше pass-2 пропустил бы. Now
    каждое occurrence yields'ится → outer loop проверяет proximity
    для каждого.
    """
    for obj, category in _OBJECT_TO_CATEGORY:
        start = 0
        while True:
            idx = window.find(obj, start)
            if idx < 0:
                break
            yield (obj, category, idx)
            start = idx + len(obj)


def _identify_claim_category(window: str) -> str | None | bool:
    """Identify the category of side-effect claimed in the window.

    Returns:
        - category name (e.g. "shopping", "reminder") if matched
        - None — claim про несуществующий tool (например cutting plan).
          Это ВСЕГДА unbacked.
        - False — нет object match в окне; не a claim вовсе.

    Substrings проверяются в порядке specificity (longer первыми
    через order в _OBJECT_TO_CATEGORY).
    """
    for obj, category in _OBJECT_TO_CATEGORY:
        if obj in window:
            return category
    return False


# 2026-06-04 (vex-assistant#100): a capability/meta QUESTION from the user —
# "что ты умеешь?", "кто ты?", "расскажи о себе". On such a turn the assistant
# is EXPECTED to describe its abilities ("ставлю напоминания, составляю меню,
# веду списки") with NO tool call, so the unbacked-claim guard must NOT fire —
# otherwise the capability answer is flagged, nudged, and collapses into the
# error fallback (prod 2026-06-04, tenant_tg_702229240: "Что ты умеешь?" ->
# "Не получилось надёжно записать это"). We gate on the USER's intent: match is
# EXACT after normalization (lowercase, ё→е, punctuation→space, filler words
# dropped), NOT substring. "что можешь сделать?" matches; "что можешь добавить в
# список?" carries an action object so it does NOT match (the guard still runs);
# "ну а вообще скажи что ты умеешь?" → fillers dropped → matches. No action-verb
# blocklist (whack-a-mole + it false-negated the canonical "что можешь сделать").
_CAPABILITY_QUESTION_PHRASES: frozenset[str] = frozenset({
    "что ты умеешь", "что умеешь", "что ты умеешь делать", "что умеешь делать",
    "что ты можешь", "что можешь", "что ты можешь делать", "что можешь делать",
    "что ты можешь сделать", "что можешь сделать", "что ты можешь сделать для меня",
    "что ты делаешь",
    "что ты за бот", "что ты за ассистент", "что ты за помощник",
    "что ты такое", "что ты такая", "что это за бот",
    "кто ты", "ты кто", "кто ты такой", "кто ты такая",
    "расскажи о себе", "расскажи про себя", "расскажи кто ты",
    "какие у тебя функции", "какие у тебя возможности",
    "какие функции", "какие возможности", "какие у тебя навыки",
    "твои возможности", "твои функции", "твои навыки", "твои умения",
    "чем ты можешь помочь", "чем можешь помочь", "чем поможешь",
    "чем ты можешь быть полезна", "чем ты можешь быть полезен",
    "для чего ты", "для чего ты нужна", "зачем ты", "зачем ты нужна",
    "зачем ты нужен", "как ты можешь помочь", "как ты работаешь",
    "как работаешь", "что ты предлагаешь",
})
# Filler tokens dropped before the exact match. MUST stay disjoint from any
# token used in the phrases above (else a real phrase would be mangled).
_CAPABILITY_QUESTION_FILLERS: frozenset[str] = frozenset({
    "а", "ну", "вообще", "пожалуйста", "плиз", "скажи", "мне",
    "эй", "слушай", "привет", "интересно", "короче", "же", "ка", "блин",
})


def is_capability_question(user_text: str) -> bool:
    """True when the user's message is a capability/meta question.

    On such a turn the assistant describes its abilities (no side-effect, no
    tool), so detect_unbacked_claim should be skipped. Matching is EXACT after
    normalization (see _CAPABILITY_QUESTION_PHRASES) — a message that carries an
    action ("что можешь добавить в список") does NOT match, so real action
    claims keep being checked.
    """
    if not user_text:
        return False
    low = user_text.lower().replace("ё", "е")
    cleaned = "".join(
        ch if (ch.isalnum() or ch.isspace()) else " " for ch in low
    )
    tokens = [t for t in cleaned.split() if t not in _CAPABILITY_QUESTION_FILLERS]
    if not tokens:
        return False
    return " ".join(tokens) in _CAPABILITY_QUESTION_PHRASES


def detect_unbacked_claim(text: str, called_tools: set[str]) -> bool:
    """Return True when the assistant text claims a side-effect but
    no corresponding write-tool was invoked this turn.

    Used after the tool-loop terminates: if this fires, the handler
    injects a nudge message and runs one more iteration asking the
    model to ACTUALLY call the tool. Bounded to one retry per turn
    to avoid runaway loops.

    2026-05-08 fix (Codex r2 CRITICAL): category-aware. Previously
    «any write-tool called → all claims OK» — это пропускало
    cross-category hallucinations (LLM вызвала shopping.add, но
    написала про крой). Теперь каждый claim проверяется на
    соответствие конкретной категории tool'ов.
    """
    if not text:
        return False
    low = text.lower()
    # 2026-05-29 (vex-assistant#79): when a checklist DISPLAY read tool was
    # called, checklist STATE-marker claims (☑/«выполнено») are backed by
    # that tool's render. Computed once, applied in Pass 2 category check.
    checklist_display_called = bool(called_tools & _CHECKLIST_DISPLAY_TOOLS)
    # Pass 1: self-asserting verbs — claim про reminder без явного
    # объекта. Проверяем что reminder-tool был вызван.
    reminder_tools = _CATEGORY_TO_TOOLS["reminder"]
    for verb in _SELF_CLAIMING_VERBS:
        if verb in low:
            if not (called_tools & reminder_tools):
                return True
    # Pass 1b (2026-05-08 r2): self-asserting actions для которых
    # НИКАКОГО tool'а не существует — любое упоминание = unbacked.
    # См. _SELF_CLAIMING_NO_TOOL для полного списка (швейные действия,
    # платежи, звонки, бронирования, отправка писем, госуслуги).
    for verb in _SELF_CLAIMING_NO_TOOL:
        if verb in low:
            return True
    # Pass 1c (2026-05-11, Codex+Xiaomi r1 on Nemotron incident):
    # 1sg FUT/PRES claim verbs ("поставлю/ставлю/добавлю/...") с
    # category check + question-context skip. Reviewer consensus —
    # эти формы критично покрыть, т.к. Nemotron в production 3 turns
    # подряд писал «поставлю напоминание» без tool call'а. НО:
    # question/offer формы («Хочешь, поставлю?») = НЕ claim. Skip
    # если sentence ends with "?" в окне 80 chars после verb ИЛИ
    # contains question-starter ("хочешь", "может", "если", "стоит
    # ли", "нужно ли", "давай") в окне 30 chars до verb.
    for verb in _CLAIM_VERBS_FUTURE:
        start = 0
        while True:
            verb_idx = low.find(verb, start)
            if verb_idx < 0:
                break
            start = verb_idx + len(verb)
            # Question / offer context check.
            before = low[max(0, verb_idx - 30): verb_idx]
            after = low[verb_idx: verb_idx + 80]
            if "?" in after:
                continue  # question form — not a claim
            if any(qs in before for qs in (
                "хочешь", "может", "если ", "стоит ли", "нужно ли",
                "давай ", "согласн", "ок если", "не против",
            )):
                continue  # offer/conditional form — not a claim
            # Treat as claim — check category via object window.
            window = low[max(0, verb_idx - 40): verb_idx + 120]
            category = _identify_claim_category(window)
            if category is False:
                continue
            if category is None:
                return True
            expected_tools = _CATEGORY_TO_TOOLS.get(category, frozenset())
            if not (called_tools & expected_tools):
                return True
    # Pass 2: verb + nearby object pair. Object → category mapping.
    # ⚠ Codex r3 MAJOR #1: iterate ВСЕ occurrences каждого verb'а,
    # не только первое. Иначе «Добавила в покупки. Добавила в план
    # кроя.» → первое окно shopping-backed → second skipped.
    #
    # ⚠ R-12 (2026-05-12, prod incident user_max_40921122 09:41):
    # window теперь ограничен sentence boundary от verb_idx (см.
    # `_intra_sentence_window`). Раньше fixed +120 chars ловил object
    # из следующего предложения (offer-question), → false positive.
    # Plus: после object match, если в 30 chars после object есть «?» —
    # это offer/question, skip.
    for verb in _CLAIM_VERBS:
        start = 0
        while True:
            verb_idx = low.find(verb, start)
            if verb_idx < 0:
                break
            # Advance start for next iteration.
            start = verb_idx + len(verb)
            # Intra-sentence window: search для object cut'ится на
            # `?`, `\n\n` от обеих сторон verb'а. Защита от
            # spillover в offer-question / paragraph break (R-12 r2).
            window, win_pre_start = _intra_sentence_window(
                low, verb_idx, len(verb),
            )
            # R-12 r4: iterate ALL object occurrences (Codex r3 MAJOR #2)
            # within proximity 30c, skip offer-question forms (Codex r3
            # MAJOR #1 — «нужно X?»/«хочешь X?» before object + `?` after).
            #
            # Past-tense `_CLAIM_VERBS` make claim regardless of trailing
            # `?` (confirmation form), но object inside offer-question
            # construct («нужно напоминание?») — это offer не claim.
            # Modal-trigger check distinguishes.
            verb_end_in_window = (verb_idx - win_pre_start) + len(verb)
            any_proximate_object = False
            for obj, category, obj_idx in _iter_claim_objects_in_window(window):
                distance = abs(obj_idx - verb_end_in_window)
                if distance > _OBJECT_PROXIMITY_CLAIM_CHARS:
                    continue  # mention/offer, not a claim
                # Per-object offer-question check: modal before + `?` after.
                if _is_offer_question_for_object(window, obj_idx, len(obj)):
                    continue  # offer-question form для этого object
                any_proximate_object = True
                if category is None:
                    # Object found, no tool exists — always unbacked.
                    # (e.g. «план кроя» — нет cutting_plan tool.)
                    return True
                # Object found, tool exists — verify matching tool was called.
                expected_tools = _CATEGORY_TO_TOOLS.get(category, frozenset())
                # 2026-05-29 (vex-assistant#79): a checklist STATE-marker
                # claim (☑/«выполнено») is backed by a checklist DISPLAY
                # read tool — its render IS the source of the marker. Only
                # for the «checklist» category and only for state-marker
                # verbs; write verbs and other categories stay write-backed.
                if (category == "checklist"
                        and checklist_display_called
                        and verb in _CHECKLIST_STATE_MARKER_VERBS):
                    expected_tools = expected_tools | _CHECKLIST_DISPLAY_TOOLS
                if not (called_tools & expected_tools):
                    return True
            # No proximate object — verb matched but objects too far
            # = mentions/offers, not claims. Skip to next verb occurrence.
            if not any_proximate_object:
                continue
    # Pass 4 (2026-05-11, Codex+Xiaomi consensus): READ-side citation
    # claims. Production incident user_tg_755682022 — модель цитировала
    # «у тебя в чек-листе X», «оттуда взяла», +14°C на 17 мая БЕЗ
    # соответствующего read-tool (called_tools=[]). Pass 1-2 ловили
    # только WRITE-side claims («сохранила/поставлю»).
    #
    # Контракт: read-verb citation phrase + object (domain noun) в окне
    # → expected read-tool ДОЛЖЕН быть в called_tools. Если нет — unbacked.
    for verb in _READ_CLAIM_VERBS:
        start = 0
        while True:
            verb_idx = low.find(verb, start)
            if verb_idx < 0:
                break
            start = verb_idx + len(verb)
            # Skip question/offer context: «у тебя в списке есть мясо?»
            # — это вопрос бота юзеру, не lying claim. См. constant
            # `_READ_QUESTION_SKIP_WINDOW` для tradeoffs.
            after = low[verb_idx: verb_idx + _READ_QUESTION_SKIP_WINDOW]
            if "?" in after:
                continue
            # Find matching object in window around verb.
            window = low[max(0, verb_idx - 30): verb_idx + 150]
            for obj, expected_tools in _READ_OBJECT_TO_TOOL:
                if obj in window:
                    if not (called_tools & expected_tools):
                        return True
                    break  # object matched + tool called → OK, next verb
    return False


# Pattern for checklist item lines в text-ответе бота.
# Matches: «— ☐ X» / «- ☑ X» / «☐ X» / «☑ X» / «✗ X» с любыми
# whitespace вокруг.
import re as _re

_CHECKLIST_ITEM_LINE_RE = _re.compile(
    r"^[\s\-—•*]*([☐☑☒✓✗])\s+(.+?)\s*$",
    _re.MULTILINE,
)


def _normalise_item_title(title: str) -> str:
    """Normalize item title for comparison: lowercase + collapse whitespace."""
    return " ".join((title or "").lower().split())


def detect_hallucinated_checklist_items(
    text: str, *, last_show_checklist_result: str | None
) -> list[str]:
    """Return a list of checklist item titles the model wrote in its
    text reply that DON'T appear in the most recent ``show_checklist``
    tool result.

    Использовался для incident'а tg_634496616 (2026-04-28): LLM писал
    «— ☐ Покрасить дом, — ☐ Чинить забор, ...» в финальном тексте,
    хотя в БД был только «Покрасить дом». Юзер видел вранье и считал
    что 4 пункта существуют.

    Args:
        text: финальный assistant text ответа.
        last_show_checklist_result: содержимое последнего ToolMessage
            от ``show_checklist`` в этом turn'е. None если show_checklist
            не вызывался — значит мы НЕ можем валидировать (LLM пишет
            из памяти, а не из tool result; это отдельная проблема —
            «mutation без verify» — ловится через prompt rule, не здесь).

    Returns:
        Список титлов в text'е которых нет в tool-result. Пустой → ОК.
    """
    if not text or not last_show_checklist_result:
        return []

    # Парсим строки в text'e: какие items LLM показывает юзеру?
    text_items: list[str] = []
    for match in _CHECKLIST_ITEM_LINE_RE.finditer(text):
        title = match.group(2).strip()
        if title:
            text_items.append(title)

    if not text_items:
        return []

    # Парсим items из tool result (формат:
    # «[clitem_xxx] ☐ Title» или похожее)
    tool_norms: set[str] = set()
    for line in last_show_checklist_result.splitlines():
        m = _CHECKLIST_ITEM_LINE_RE.search(line)
        if m:
            tool_norms.add(_normalise_item_title(m.group(2)))
        # Fallback: «[clitem_id] ☐ X» формат
        if "[clitem_" in line:
            for marker in ("☐", "☑", "☒", "✓", "✗"):
                idx = line.find(marker)
                if idx >= 0:
                    tail = line[idx + 1:].strip()
                    if tail:
                        tool_norms.add(_normalise_item_title(tail))
                    break

    hallucinated: list[str] = []
    for title in text_items:
        if _normalise_item_title(title) not in tool_norms:
            hallucinated.append(title)
    return hallucinated


# Supported chat-LLM providers. Extend this tuple AFTER adding a build
# branch in ``_build_chat_llm`` and a matching setting block in
# ``config.settings``; the handler layer treats unknown providers as
# "LLM disabled" rather than crashing the turn.
CHAT_PROVIDERS = (
    "mimo",
    # 2026-05-29 rename (Boris) — provider key now == model name, no more
    # "mimo-v2.5 secretly means -pro" trap. OLD "mimo-v2.5"→-pro became
    # "mimo-v2.5-pro"; OLD "mimo-v2.5-light"→plain became "mimo-v2.5".
    "mimo-v2.5-pro",         # mimo-v2.5-pro — Xiaomi's next-gen baseline (was key "mimo-v2.5")
    "mimo-v2.5",             # plain mimo-v2.5 — lighter, ~2x faster (was key "mimo-v2.5-light")
    "mimo-flash",            # 2026-05-23: mimo-v2-flash — fastest MiMo (v2 series),
                             # latency test candidate for Boris's account (#65)
    "openrouter",            # gemma-4-26b-a4b-it (default), verified fast
    "openrouter-grok",       # x-ai/grok-4.1-fast — "lowest hallucination" claim
    "openrouter-qwen",       # qwen/qwen3.6-plus — clean runner-up in bench
    # 2026-05-18: gemini/deepseek variants restored for controlled LLM
    # comparison runs. Keep them explicit so admin/provider switches do
    # not rely on ad-hoc model strings.
    "openrouter-gemini-lite",    # google/gemini-3.1-flash-lite — thinking levels, 1M ctx
    "openrouter-deepseek",       # deepseek/deepseek-v4-flash — MoE 284B/13B, 1M ctx
    # 2026-05-12: 3 экспериментальных провайдеров удалены после серии
    # бенчей и прод-инцидентов (gpt-oss-120b, llama-70b, llama-4-scout).
    # Не показали value над mimo/gemini-2.5-flash; держать их в admin UI =
    # риск misconfiguration.
    # 2026-05-10 PM: Nemotron 3 Super — DeepInfra/bf16, hybrid Mamba-Transformer
    # MoE 117B/12B active, 262k ctx. Bench показал 0.88s avg (10× быстрее mimo,
    # 3.7× быстрее gemini-lite), $0.0031/turn (+35% от mimo). Прошёл D.tool_error
    # (gemini/llama не прошли — пустой reply). Soul-v3 пофиксил «вы» → «ты».
    # Кандидат на primary через runtime_config switcher.
    "openrouter-nemotron-3-super",
    # 2026-05-12: qwen3.5-flash — Boris choice for primary replacement.
    # Bench: 2.7s avg wall (5× mimo), $0.0061/turn (18% cheaper than mimo),
    # tool calling reliable on 7/8 scenarios (H.poisoned_history — fake
    # «Поставлю напоминание» без schedule_reminder; в проде Pass 1c
    # detector ловит → nudge retry).
    "openrouter-qwen-flash",
    # 2026-05-12: Gemini 2.5 Flash via OR — fastest reliable provider в нашем
    # бенче (1.5s avg, all 8 scenarios tools clean). $0.30/$2.50 per 1M
    # (OR +5.5% markup vs direct Google). Pro tier candidate / mimo fallback.
    "openrouter-gemini-2.5-flash",
    # 2026-06-10 (#107, выбор Бориса): кандидат в РОТ против лёгкой mimo-v2.5
    # — сверхдешёвый lite-вариант 2.5 flash.
    "openrouter-gemini-2.5-flash-lite",
    # 2026-06-15 (#60): Inception Mercury-2 (прямой api.inceptionlabs.ai) —
    # диффузионная LLM, 1000+ tps + cached input. Переезд планировщика с MiMo.
    "inception-mercury2",         # mercury-2
    # 2026-06-19 (#173, форк freddie-eval): 3 кандидата на замену/фоллбек Фредди.
    # ТОЛЬКО для оценки — на прод не катим. Замер через SREDA_PLANNER_PROVIDER.
    "openrouter-trinity-thinking",   # arcee-ai/trinity-large-thinking
    "openrouter-ling-flash",         # inclusionai/ling-2.6-flash
    "openrouter-qwen3-thinking",     # qwen/qwen3-next-80b-a3b-thinking
    # 2026-06-19 (#173): быстрые НЕ-thinking, пиннинг Groq (форс быстрый бэкенд):
    "openrouter-llama33-groq",       # llama-3.3-70b @ Groq
    "openrouter-llama4scout-groq",   # llama-4-scout @ Groq
    # 2026-06-19 (#173): ПРЯМОЙ Groq (свой ключ, без OpenRouter):
    "groq-llama33-70b",              # llama-3.3-70b-versatile
    "groq-gpt-oss-120b",             # openai/gpt-oss-120b
    "groq-gpt-oss-20b",              # openai/gpt-oss-20b
    "groq-qwen36-27b",               # qwen/qwen3.6-27b
    "groq-qwen3-32b",                # qwen/qwen3-32b
    "groq-llama4-scout",             # meta-llama/llama-4-scout-17b-16e-instruct
    "groq-llama31-8b",               # llama-3.1-8b-instant
)

# MiMo variants share base_url + api key — only the model id changes.
# Same pattern as ``_OPENROUTER_MODEL_BY_PROVIDER`` below: one dict
# entry per exposed provider key.
_MIMO_MODEL_BY_PROVIDER = {
    "mimo": None,                      # None = fall back to settings.mimo_chat_model
    # 2026-05-29 rename (Boris): provider key == model name (1:1).
    "mimo-v2.5-pro": "mimo-v2.5-pro",  # pro tier (was key "mimo-v2.5")
    "mimo-v2.5": "mimo-v2.5",          # plain/light, simple tasks (was key "mimo-v2.5-light")
    "mimo-flash": "mimo-v2-flash",     # v2 series, fastest Xiaomi tier — listed
                                       # on /v1/models 2026-05-23 (token-plan ⇒ pay-per-use endpoint)
}

# OpenRouter variants share the same base_url + api key — they differ
# only in which model is invoked. Keeping the mapping here means
# adding a 4th/5th variant is a single dict entry in both the
# build-dispatch and the admin UI metadata.
_OPENROUTER_MODEL_BY_PROVIDER = {
    "openrouter": None,  # None = fall back to settings.openrouter_chat_model
    "openrouter-grok": "x-ai/grok-4.1-fast",
    "openrouter-qwen": "qwen/qwen3.6-plus",
    "openrouter-gemini-lite":       "google/gemini-3.1-flash-lite",
    "openrouter-deepseek":          "deepseek/deepseek-v4-flash",
    # 2026-05-12: 3 stale provider keys removed (gpt-oss-120b, llama-70b,
    # llama-4-scout). См. CHAT_PROVIDERS comment.
    "openrouter-nemotron-3-super":  "nvidia/nemotron-3-super-120b-a12b",
    # 2026-05-12: qwen3.5-flash hybrid linear-attention + sparse MoE,
    # 1M context. Cheapest tool-calling provider в нашем бенче.
    "openrouter-qwen-flash":        "qwen/qwen3.5-flash-02-23",
    "openrouter-gemini-2.5-flash":  "google/gemini-2.5-flash",
    "openrouter-gemini-2.5-flash-lite": "google/gemini-2.5-flash-lite",
    # 2026-06-19 (#173, форк freddie-eval) — кандидаты на замену/фоллбек Фредди (оценка):
    "openrouter-trinity-thinking":  "arcee-ai/trinity-large-thinking",
    "openrouter-ling-flash":        "inclusionai/ling-2.6-flash",
    "openrouter-qwen3-thinking":    "qwen/qwen3-next-80b-a3b-thinking",
    "openrouter-llama33-groq":      "meta-llama/llama-3.3-70b-instruct",
    "openrouter-llama4scout-groq":  "meta-llama/llama-4-scout",
}


# Inception (прямой) — Mercury-2. id сверен по /v1/models на ключе 2026-06-15.
_INCEPTION_MODEL_BY_PROVIDER = {
    "inception-mercury2": "mercury-2",
}


# 2026-06-19 (#173): ПРЯМОЙ Groq (OpenAI-совместимый), ключ = resolve_groq_api_key()
# (тот же, что для Whisper-STT). Tool-use модели сверены по /openai/v1/models на ключе.
# Без хопа OpenRouter → ниже латентность. ТОЛЬКО для оценки замены Фредди.
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_GROQ_MODEL_BY_PROVIDER = {
    "groq-llama33-70b":   "llama-3.3-70b-versatile",
    "groq-gpt-oss-120b":  "openai/gpt-oss-120b",
    "groq-gpt-oss-20b":   "openai/gpt-oss-20b",
    "groq-qwen36-27b":    "qwen/qwen3.6-27b",
    "groq-qwen3-32b":     "qwen/qwen3-32b",
    "groq-llama4-scout":  "meta-llama/llama-4-scout-17b-16e-instruct",
    "groq-llama31-8b":    "llama-3.1-8b-instant",
}


# 2026-05-10: Per-provider OpenRouter routing overrides (extra_body).
# OpenRouter обычно сам выбирает provider'а с лучшей ценой, но иногда
# нам нужно явно зафиксить конкретный backend (quantization, GPU type,
# region). Передаётся в ChatOpenAI(extra_body={"provider": {...}}).
# Docs: https://openrouter.ai/docs/features/provider-routing
#
# Nemotron: Boris explicit choice — DeepInfra/bf16 (а не fp8 / другие
# DeepInfra варианты). Бенч 2026-05-10 проводился именно на этом backend'е.
# 2026-05-11 PM: Per-provider temperature override. Default 0.3 (из
# get_chat_llm signature) подходит для mimo/openrouter generic — модель
# нормально варьирует ответы. Но reasoning-capable модели (Nemotron)
# на 0.3 уходят в стохастические drift-режимы (English greeting,
# tool simulation в content, reasoning leak). Probe с 0.2 работает,
# 0.3 ломается — снижение усиливает determinism для tool dispatch.
# Application: _build_chat_llm читает _override_temperature(provider)
# и заменяет caller-provided value если override установлен.
_OPENROUTER_TEMPERATURE_BY_PROVIDER: dict[str, float] = {
    # Nemotron сильно зависит от temperature (probe 0.2 vs prod 0.3 dichotomy).
    # 0.1 — agressive determinism для повышения tool-dispatch reliability.
    "openrouter-nemotron-3-super": 0.1,
    # 2026-06-15: Mercury ОТВЕРГАЕТ temp<0.5 («not within [0.5,1]» → форсит 0.75).
    # 0.6 = минимум допустимого диапазона + чуть детерминизма для планировщика.
    "inception-mercury2": 0.6,
}


def _override_temperature(provider: str, default: float) -> float:
    """Return per-provider temperature override or default."""
    return _OPENROUTER_TEMPERATURE_BY_PROVIDER.get(provider, default)


_OPENROUTER_EXTRA_BODY_BY_PROVIDER: dict[str, dict] = {
    # 2026-06-19 (#173): форс Groq (быстрый бэкенд, ~500-1000 tps) для замера скорости.
    "openrouter-llama33-groq": {
        "provider": {"only": ["groq"], "allow_fallbacks": False},
    },
    "openrouter-llama4scout-groq": {
        "provider": {"only": ["groq"], "allow_fallbacks": False},
    },
    # 2026-05-11: `order` оказался preference, не forced — OpenRouter
    # роутил на DekaLLM (нестабильный, 1.9-253 t/s spread) когда
    # DeepInfra/bf16 был busy. Меняем на `only` + allow_fallbacks=False
    # чтобы гарантировать DeepInfra/bf16 или 5xx (после которой langchain
    # автоматом перейдёт на mimo fallback chain).
    "openrouter-nemotron-3-super": {
        "provider": {
            "only": ["deepinfra/bf16"],  # 2026-05-11: lowercase slug per OpenRouter API (was PascalCase → NotFoundError)
            "allow_fallbacks": False,
        },
        # 2026-05-11: Nemotron has reasoning ON by default. Probe trace
        # 2026-05-10 21:15 (Boris reminder request) showed model burned
        # 1173 reasoning_tokens, then dropped to default English greeting
        # «Hello! 👋 How can I help you today?» without calling
        # schedule_reminder. With reasoning OFF, Nemotron makes correct
        # tool call (schedule_reminder with proper args) in 64 completion
        # tokens. Soul-v3 + reasoning OFF = production-ready combo.
        #
        # 2026-05-11 PM (Xiaomi r1 MAJOR): добавил max_tokens:0 как
        # belt-and-suspenders — некоторые backend'ы обрабатывают
        # `enabled:false` как hint, а `max_tokens:0` как hard limit
        # на reasoning generation. Уменьшает probability reasoning
        # leak в content field (FM3 на проде 21:42).
        "reasoning": {"enabled": False, "max_tokens": 0},
    },
    # 2026-05-12: qwen3.5-flash. Bench показывал чистое поведение без
    # reasoning, но Qwen-line has thinking capability — выключаем явно
    # по тому же принципу что и Nemotron (избегаем drift modes на проде).
    "openrouter-qwen-flash": {
        "reasoning": {"enabled": False},
    },
}


def _build_chat_llm(
    provider: str,
    settings: Settings,
    *,
    model: str | None,
    temperature: float,
    **kwargs: Any,
) -> ChatOpenAI | None:
    """Construct a ``ChatOpenAI`` for the named provider or return None
    if the provider isn't configured (missing key, unknown name).

    Keeping the per-provider wiring in one helper lets
    ``get_chat_llm`` stay small and makes adding a provider a single
    new ``if`` branch.
    """
    if provider in _MIMO_MODEL_BY_PROVIDER:
        api_key = settings.resolve_mimo_api_key()
        if not api_key:
            logger.info("chat LLM disabled: no MiMo API key configured")
            return None
        override = _MIMO_MODEL_BY_PROVIDER[provider]
        # R-29: use MimoChatOpenAI subclass to preserve mimo's
        # `reasoning_content` (thinking mode) per mimo API contract.
        # Standard ChatOpenAI ignores this field → iter.1+ mimo rejects
        # 400 «must be passed back» → fallback engaged → confab class.
        # See sreda.services.mimo_chat_openai for details.
        from sreda.services.mimo_chat_openai import MimoChatOpenAI
        return MimoChatOpenAI(
            base_url=settings.mimo_base_url,
            api_key=api_key,
            # Explicit ``model=`` beats per-variant override beats
            # settings default. ``mimo`` → ``settings.mimo_chat_model``
            # (currently ``mimo-v2-pro``); ``mimo-v2.5`` → ``mimo-v2.5-pro``.
            model=model or override or settings.mimo_chat_model,
            temperature=temperature,
            timeout=settings.mimo_request_timeout_seconds,
            **kwargs,
        )
    if provider in _OPENROUTER_MODEL_BY_PROVIDER:
        api_key = settings.resolve_openrouter_api_key()
        if not api_key:
            logger.info("chat LLM disabled: no OpenRouter API key configured")
            return None
        override = _OPENROUTER_MODEL_BY_PROVIDER[provider]
        # 2026-05-10: per-provider extra_body для forced routing
        # (например DeepInfra/bf16 на nemotron). Объединяем с любым
        # extra_body переданным от вызывающего (kwargs.pop), чтобы не
        # затереть. Caller'ский имеет приоритет.
        extra_body_default = _OPENROUTER_EXTRA_BODY_BY_PROVIDER.get(provider)
        caller_extra_body = kwargs.pop("extra_body", None)
        if extra_body_default and caller_extra_body:
            # caller overrides: shallow merge, caller wins on key conflict
            merged = {**extra_body_default, **caller_extra_body}
            kwargs["extra_body"] = merged
        elif caller_extra_body:
            kwargs["extra_body"] = caller_extra_body
        elif extra_body_default:
            kwargs["extra_body"] = extra_body_default
        return ChatOpenAI(
            base_url=settings.openrouter_base_url,
            api_key=api_key,
            # Explicit ``model=`` arg (bench probes) beats per-provider
            # override beats settings default. Lets one config hit
            # multiple targets without per-variant settings plumbing.
            model=model or override or settings.openrouter_chat_model,
            # 2026-05-11 PM: per-provider temperature override.
            # Nemotron specifically needs lower temp (0.1) to avoid drift.
            temperature=_override_temperature(provider, temperature),
            timeout=settings.mimo_request_timeout_seconds,
            **kwargs,
        )
    if provider in _INCEPTION_MODEL_BY_PROVIDER:
        api_key = settings.resolve_inception_api_key()
        if not api_key:
            logger.info("chat LLM disabled: no Inception API key configured")
            return None
        override = _INCEPTION_MODEL_BY_PROVIDER[provider]
        return ChatOpenAI(
            base_url=settings.inception_base_url,
            api_key=api_key,
            model=model or override,
            temperature=_override_temperature(provider, temperature),
            timeout=settings.mimo_request_timeout_seconds,
            **kwargs,
        )
    if provider in _GROQ_MODEL_BY_PROVIDER:  # #173 прямой Groq (оценка)
        api_key = settings.resolve_groq_api_key()
        if not api_key:
            logger.info("chat LLM disabled: no Groq API key configured")
            return None
        override = _GROQ_MODEL_BY_PROVIDER[provider]
        return ChatOpenAI(
            base_url=_GROQ_BASE_URL,
            api_key=api_key,
            model=model or override,
            temperature=_override_temperature(provider, temperature),
            timeout=settings.mimo_request_timeout_seconds,
            **kwargs,
        )
    logger.warning("chat LLM: unknown provider %r — ignoring", provider)
    return None


# Once-per-process flag so a persistent DB problem (missing table,
# revoked permissions) doesn't spam a full traceback on every chat
# turn. First hit logs at WARNING with the cause; subsequent hits
# are silent until the process restarts.
_RUNTIME_CONFIG_WARNED = False


def resolve_provider_pair(settings: Settings | None = None) -> tuple[str, str | None]:
    """Public wrapper для _resolve_provider_overrides.

    Возвращает (primary_provider_name, fallback_provider_name_or_None) на
    основе runtime_config (admin live-switch) и settings (env). Используется
    в handlers.py для построения отдельных primary + fallback LLM
    клиентов под per-call timeout с ручной fallback логикой.
    """
    s = settings or get_settings()
    return _resolve_provider_overrides(s)


def resolve_provider_pair_for_tenant(
    session: Any,
    tenant_id: str,
    settings: Settings | None = None,
) -> tuple[str, str | None]:
    """Resolve primary/fallback provider for one tenant.

    Tenant override is intentionally evaluated with the caller's DB session so
    one chat turn can snapshot provider routing once instead of letting primary
    and fallback rebuild paths re-read runtime_config through independent
    sessions.
    """

    s = settings or get_settings()
    try:
        primary, fallback = _resolve_provider_overrides_from_session(session, s)
    except Exception:  # noqa: BLE001
        logger.exception(
            "chat LLM: global provider overrides unavailable tenant=%s",
            tenant_id,
        )
        return s.chat_provider, s.chat_fallback_provider
    try:
        from sreda.services import runtime_config as rc

        raw = rc.get_config(session, rc.KEY_CHAT_PROVIDER_TENANT_OVERRIDES)
    except Exception:  # noqa: BLE001
        logger.exception(
            "chat LLM: tenant provider overrides unavailable tenant=%s",
            tenant_id,
        )
        return primary, fallback
    if not raw:
        return primary, fallback
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "chat LLM: invalid tenant provider overrides JSON tenant=%s",
            tenant_id,
        )
        return primary, fallback
    tenant_override = overrides.get(tenant_id) if isinstance(overrides, dict) else None
    if not isinstance(tenant_override, dict):
        return primary, fallback
    override_primary = str(tenant_override.get("primary") or "").strip()
    override_fallback_raw = tenant_override.get("fallback")
    override_fallback = (
        None if override_fallback_raw is None
        else str(override_fallback_raw).strip() or None
    )
    if not _provider_key_is_known(override_primary):
        logger.warning(
            "chat LLM: unknown tenant primary provider tenant=%s provider=%r",
            tenant_id, override_primary,
        )
        return primary, fallback
    if override_fallback and not _provider_key_is_known(override_fallback):
        logger.warning(
            "chat LLM: unknown tenant fallback provider tenant=%s provider=%r",
            tenant_id, override_fallback,
        )
        return primary, fallback
    if override_fallback == override_primary:
        logger.warning(
            "chat LLM: tenant fallback equals primary tenant=%s provider=%s",
            tenant_id, override_primary,
        )
        return primary, fallback
    if not _provider_key_is_available(override_primary, s):
        logger.warning(
            "chat LLM: tenant primary provider unavailable tenant=%s provider=%s",
            tenant_id, override_primary,
        )
        return primary, fallback
    if override_fallback and not _provider_key_is_available(override_fallback, s):
        logger.warning(
            "chat LLM: tenant fallback provider unavailable tenant=%s provider=%s",
            tenant_id, override_fallback,
        )
        return primary, fallback
    return override_primary, override_fallback


def _provider_key_is_known(provider: str) -> bool:
    return provider in CHAT_PROVIDERS


def _provider_key_is_available(provider: str, settings: Settings) -> bool:
    if provider in _MIMO_MODEL_BY_PROVIDER:
        return bool(settings.resolve_mimo_api_key())
    if provider in _OPENROUTER_MODEL_BY_PROVIDER:
        return bool(settings.resolve_openrouter_api_key())
    if provider in _INCEPTION_MODEL_BY_PROVIDER:
        return bool(settings.resolve_inception_api_key())
    if provider in _GROQ_MODEL_BY_PROVIDER:  # #173 прямой Groq
        return bool(settings.resolve_groq_api_key())
    return False


def _resolve_provider_overrides(settings: Settings) -> tuple[str, str | None]:
    """Consult the admin-switcher DB table for live overrides, falling
    back to env-var-based Settings when a key isn't set. Returns
    ``(primary, fallback_or_None)``.

    An empty-string override in the DB is treated as "explicitly
    disable" — useful when the admin wants to kill the fallback chain
    without nulling the setting entirely.

    DB errors (missing table, locked db, etc.) degrade silently to
    env defaults so a half-migrated install still serves turns.
    """
    global _RUNTIME_CONFIG_WARNED

    try:
        from sqlalchemy.exc import OperationalError, ProgrammingError

        from sreda.db.session import get_session_factory
    except ImportError:
        return settings.chat_provider, settings.chat_fallback_provider

    try:
        session = get_session_factory()()
    except Exception:  # noqa: BLE001 — session factory not ready yet
        return settings.chat_provider, settings.chat_fallback_provider
    try:
        return _resolve_provider_overrides_from_session(session, settings)
    except (OperationalError, ProgrammingError) as exc:
        if not _RUNTIME_CONFIG_WARNED:
            logger.warning(
                "chat LLM: runtime_config unavailable (%s) — using env defaults; "
                "create the table via Base.metadata.create_all to enable the "
                "admin LLM-switcher. This warning fires once per process.",
                type(exc).__name__,
            )
            _RUNTIME_CONFIG_WARNED = True
        return settings.chat_provider, settings.chat_fallback_provider
    finally:
        session.close()


def _resolve_provider_overrides_from_session(
    session: Any,
    settings: Settings,
) -> tuple[str, str | None]:
    primary = settings.chat_provider
    fallback = settings.chat_fallback_provider
    from sreda.services import runtime_config as rc

    db_primary = rc.get_config(session, rc.KEY_CHAT_PROVIDER)
    db_fallback = rc.get_config(session, rc.KEY_CHAT_FALLBACK_PROVIDER)
    if db_primary:
        primary = db_primary
    if db_fallback is not None:
        # Empty string = explicit "no fallback".
        fallback = db_fallback or None
    return primary, fallback


def get_chat_llm(
    settings: Settings | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    with_fallback: bool = False,
    **kwargs: Any,
) -> Any | None:
    """Build a chat-LLM client pointed at the configured provider.

    Returns ``None`` when no provider is available (no key configured,
    or an unknown provider name). Callers must tolerate this gracefully
    and short-circuit the turn with a "LLM disabled" reply — crashing
    hurts UX more than admitting the limitation.

    Resolution order for the provider name:
      1. Explicit ``provider=`` argument (bench/probe use).
      2. ``runtime_config.chat_primary_provider`` (admin live-switch).
      3. ``settings.chat_provider`` (env-var / default).

    Parameters
    ----------
    provider :
        Override for the resolved provider. Skips the admin-switcher
        DB lookup entirely — intended for bench tools and hot-probing
        a specific backend without mutating persistent state.
    with_fallback :
        When True and a fallback provider is configured (via admin
        switcher or env), wraps the primary runnable with LangChain's
        ``.with_fallbacks([...])``. The fallback kicks in on ANY
        exception from the primary — rate limits, timeouts, 5xx — and
        replays the same message list against the backup transparently.
        One level deep on purpose; three-tier would hide the freshly-
        interesting failure mode.
    """
    settings = settings or get_settings()
    if provider is not None:
        effective_primary = provider
        effective_fallback = None  # bench/probe: fallback irrelevant
    else:
        effective_primary, effective_fallback = _resolve_provider_overrides(settings)
    primary_llm = _build_chat_llm(
        effective_primary, settings,
        model=model, temperature=temperature, **kwargs,
    )
    if primary_llm is None:
        return None
    if not with_fallback or not effective_fallback:
        return primary_llm
    if effective_fallback == effective_primary:
        # Fallback same as primary is a no-op and a config smell.
        logger.warning(
            "chat LLM: fallback provider equals primary (%s) — skipping wrap",
            effective_primary,
        )
        return primary_llm
    fallback_llm = _build_chat_llm(
        effective_fallback, settings,
        model=None,  # fallback uses its provider's default model
        temperature=temperature, **kwargs,
    )
    if fallback_llm is None:
        logger.warning(
            "chat LLM: fallback provider %r not configured — primary-only",
            effective_fallback,
        )
        return primary_llm
    logger.info(
        "chat LLM: wrapping %s with fallback → %s",
        effective_primary, effective_fallback,
    )
    return primary_llm.with_fallbacks([fallback_llm])
