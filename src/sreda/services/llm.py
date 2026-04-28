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

import concurrent.futures
import logging
import re
import threading
from typing import Any

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
                "запланировал", "запланировала")
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
                  "☐", "☑", "☒", "✗")


def detect_unbacked_claim(text: str, called_tools: set[str]) -> bool:
    """Return True when the assistant text claims a side-effect but
    no corresponding write-tool was invoked this turn.

    Used after the tool-loop terminates: if this fires, the handler
    injects a nudge message and runs one more iteration asking the
    model to ACTUALLY call the tool. Bounded to one retry per turn
    to avoid runaway loops.
    """
    if not text:
        return False
    # Any write-tool call counts as backing — we don't try to map
    # specific verb → specific tool, that's fragile across wording.
    if called_tools & _WRITE_TOOL_NAMES:
        return False
    low = text.lower()
    for verb in _CLAIM_VERBS:
        verb_idx = low.find(verb)
        if verb_idx < 0:
            continue
        # Limit the object-search window so "я сохранил то что ты
        # сказала — готово, меню не трогай" doesn't false-fire from
        # a distant "меню" mention.
        window = low[max(0, verb_idx - 40): verb_idx + 120]
        if any(obj in window for obj in _CLAIM_OBJECTS):
            return True
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
    "mimo-v2.5",             # mimo-v2.5-pro — Xiaomi's next-gen baseline
    "mimo-v2.5-light",       # mimo-v2.5 (no -pro) — lighter variant, ~2x faster
    "openrouter",            # gemma-4-26b-a4b-it (default), verified fast
    "openrouter-grok",       # x-ai/grok-4.1-fast — "lowest hallucination" claim
    "openrouter-qwen",       # qwen/qwen3.6-plus — clean runner-up in bench
)

# MiMo variants share base_url + api key — only the model id changes.
# Same pattern as ``_OPENROUTER_MODEL_BY_PROVIDER`` below: one dict
# entry per exposed provider key.
_MIMO_MODEL_BY_PROVIDER = {
    "mimo": None,                      # None = fall back to settings.mimo_chat_model
    "mimo-v2.5": "mimo-v2.5-pro",
    "mimo-v2.5-light": "mimo-v2.5",    # light variant for simple tasks
}

# OpenRouter variants share the same base_url + api key — they differ
# only in which model is invoked. Keeping the mapping here means
# adding a 4th/5th variant is a single dict entry in both the
# build-dispatch and the admin UI metadata.
_OPENROUTER_MODEL_BY_PROVIDER = {
    "openrouter": None,  # None = fall back to settings.openrouter_chat_model
    "openrouter-grok": "x-ai/grok-4.1-fast",
    "openrouter-qwen": "qwen/qwen3.6-plus",
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
        return ChatOpenAI(
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
        return ChatOpenAI(
            base_url=settings.openrouter_base_url,
            api_key=api_key,
            # Explicit ``model=`` arg (bench probes) beats per-provider
            # override beats settings default. Lets one config hit
            # multiple targets without per-variant settings plumbing.
            model=model or override or settings.openrouter_chat_model,
            temperature=temperature,
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

    primary = settings.chat_provider
    fallback = settings.chat_fallback_provider
    try:
        from sqlalchemy.exc import OperationalError, ProgrammingError

        from sreda.db.session import get_session_factory
        from sreda.services import runtime_config as rc
    except ImportError:
        return primary, fallback

    try:
        session = get_session_factory()()
    except Exception:  # noqa: BLE001 — session factory not ready yet
        return primary, fallback
    try:
        db_primary = rc.get_config(session, rc.KEY_CHAT_PROVIDER)
        db_fallback = rc.get_config(session, rc.KEY_CHAT_FALLBACK_PROVIDER)
    except (OperationalError, ProgrammingError) as exc:
        if not _RUNTIME_CONFIG_WARNED:
            logger.warning(
                "chat LLM: runtime_config unavailable (%s) — using env defaults; "
                "create the table via Base.metadata.create_all to enable the "
                "admin LLM-switcher. This warning fires once per process.",
                type(exc).__name__,
            )
            _RUNTIME_CONFIG_WARNED = True
        return primary, fallback
    finally:
        session.close()

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
