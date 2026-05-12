"""Benchmark harness for chat LLM providers.

Runs a set of canonical agent-turn scenarios against each configured
provider, measures wall-time per iteration, counts tool_calls, and
prints a comparison table. Purpose: decide which provider to A/B in
production for Сpeda's housewife skill.

Not unit-tested: this is a research-time script, meant to be re-run
manually whenever we re-benchmark.

Run from repo root:

    python scripts/dev/benchmark_llm_providers.py

Reads provider secrets from:
    .secrets/mimo_api_key.txt
    .secrets/cerebras_chat_token.txt

Each scenario is a (system_prompt + user_text + tools) setup. The
runner drives a simple tool-loop (max 4 iterations) to completion
and records end-to-end wall time and token counts. Scenarios are
chosen to reflect real production turn shapes we've observed:

    A) Single read  — 'что у меня в списке покупок'
    B) Multi read   — 'что в списке и что в меню на сегодня'
    C) Batch write  — 'сохрани 5 рецептов: борщ, омлет, плов, котлеты, блины'
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from sreda.runtime.handlers import build_system_prompt


# ---------------------------------------------------------------------------
# Provider matrix
# ---------------------------------------------------------------------------


def _read_secret(rel_path: str) -> str | None:
    p = Path(rel_path)
    if not p.exists():
        return None
    val = p.read_text(encoding="utf-8").strip()
    return val or None


def _openrouter_key() -> str | None:
    # OpenRouter token file is markdown with the bare key on line 1.
    p = Path(".secrets/openrouter-token.md")
    if not p.exists():
        return None
    first_line = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    return first_line or None


PROVIDERS = [
    # Baseline — legacy MiMo-V2-Pro (Сяоми Preferential rate 2026-04-28:
    # 2 credits / token = $0.08/M на 1x rate, но V2-Pro был 2x → $0.16/M
    # эквивалентно. Сохраняем для исторического сравнения).
    {
        "label": "MiMo-V2-Pro",
        "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
        "api_key": _read_secret(".secrets/mimo_api_key.txt"),
        "model": "mimo-v2-pro",
        "price_in": 0.16, "price_out": 0.16,
    },
    # Текущий production primary — MiMo-V2.5-Pro. Сяоми Preferential
    # rate (2026-04-28): 1 token = 2 credits = $0.08/M (по сообщению
    # юзера про rate $0.08/M token). 2x rate (Pro) уже учтён в credit
    # → реальная плата ~$0.08/M. Off-peak –20% в 16-24 UTC.
    {
        "label": "MiMo-V2.5-Pro",
        "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
        "api_key": _read_secret(".secrets/mimo_api_key.txt"),
        "model": "mimo-v2.5-pro",
        "price_in": 0.08, "price_out": 0.08,
    },
    # MiMo V2.5 (light, ~2x быстрее). 1x rate = $0.04/M эквивалент.
    {
        "label": "MiMo-V2.5",
        "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
        "api_key": _read_secret(".secrets/mimo_api_key.txt"),
        "model": "mimo-v2.5",
        "price_in": 0.04, "price_out": 0.04,
    },
    # OpenRouter paid candidates (prices per 1M tokens, verified
    # against /api/v1/models on 2026-04-22).
    {
        "label": "OR/gemma-4-26b-moe",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemma-4-26b-a4b-it",
        "price_in": 0.07, "price_out": 0.35,
    },
    {
        "label": "OR/gemma-4-31b",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemma-4-31b-it",
        "price_in": 0.13, "price_out": 0.38,
    },
    {
        "label": "OR/grok-4.1-fast",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "x-ai/grok-4.1-fast",
        "price_in": 0.20, "price_out": 0.50,
    },
    {
        "label": "OR/minimax-m2.7",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "minimax/minimax-m2.7",
        "price_in": 0.30, "price_out": 1.20,
    },
    {
        "label": "OR/qwen3.6-plus",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "qwen/qwen3.6-plus",
        "price_in": 0.33, "price_out": 1.95,
    },
    {
        "label": "OR/kimi-k2.5",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "moonshotai/kimi-k2.5",
        "price_in": 0.44, "price_out": 2.00,
    },
    # 2026-04-28: DeepSeek V4 Flash — релиз 24.04, MoE 284B/13B
    # активных, 1M context. Конкурент MiMo по цене / скорости.
    # Поддерживает structured tool calling.
    {
        "label": "OR/deepseek-v4-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "deepseek/deepseek-v4-flash",
        "price_in": 0.14, "price_out": 0.28,
    },
    # 2026-05-10: Cost/speed candidates — 5 моделей для A/B vs mimo.
    # Все verified через OpenRouter API на дату добавления:
    # tools-capable, ≥131k context, output ≤$1.50/M.
    {
        "label": "OR/gpt-oss-120b",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "openai/gpt-oss-120b",
        "price_in": 0.039, "price_out": 0.180,
    },
    {
        "label": "OR/llama-3.3-70b",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "meta-llama/llama-3.3-70b-instruct",
        "price_in": 0.10, "price_out": 0.32,
    },
    {
        "label": "OR/llama-4-scout",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "meta-llama/llama-4-scout",
        "price_in": 0.08, "price_out": 0.30,
    },
    {
        "label": "OR/gemini-3.1-flash-lite",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemini-3.1-flash-lite",
        "price_in": 0.25, "price_out": 1.50,
    },
    # 2026-05-10: Nemotron 3 Super — NVIDIA hybrid Mamba-Transformer
    # MoE 120B/12B active, 262k ctx. Через OpenRouter с принудительным
    # routing на DeepInfra/bf16 (Boris explicit choice).
    # extra_body forces provider preference; OpenRouter docs:
    # https://openrouter.ai/docs/features/provider-routing
    {
        "label": "OR/nemotron-3-super-120b",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "price_in": 0.09, "price_out": 0.45,
        # 2026-05-11: `order` это preference (fallback на любой если занят);
        # `only`+allow_fallbacks=False — жёсткий pin (либо тот провайдер,
        # либо ошибка). Bench работал на `order` потому что DeepInfra/bf16
        # был свободен; на проде роуталось на DekaLLM (1.9-253 t/s spread).
        "extra_body": {
            "provider": {
                "only": ["deepinfra/bf16"],  # lowercase slug
                "allow_fallbacks": False,
            },
            # 2026-05-11: Nemotron reasoning ON eats 1000+ tokens and
            # drops to English greeting on complex requests. OFF = clean.
            "reasoning": {"enabled": False},
        },
    },
    # 2026-05-11 round 2: Boris explicit ask — kimi-k2.6 / hy3-preview /
    # gemini-3-flash-preview. Forced fast provider routing via extra_body
    # (mirror Nemotron pattern). Reasoning OFF на всех — Nemotron incident
    # показал что drift modes коррелируют с reasoning ON.
    {
        "label": "OR/kimi-k2.6",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "moonshotai/kimi-k2.6",
        "price_in": 0.75, "price_out": 3.50,
        "extra_body": {
            "provider": {"only": ["fireworks"], "allow_fallbacks": False},
            "reasoning": {"enabled": False},
        },
    },
    {
        "label": "OR/hy3-preview",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "tencent/hy3-preview",
        "price_in": 0.066, "price_out": 0.260,
        "extra_body": {
            "provider": {"only": ["siliconflow"], "allow_fallbacks": False},
            # Hy3 supports "configurable reasoning levels: disabled/low/high".
            "reasoning": {"enabled": False},
        },
    },
    {
        "label": "OR/gemini-3-flash-preview",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemini-3-flash-preview",
        "price_in": 0.50, "price_out": 3.00,
        "extra_body": {
            "provider": {"only": ["google-ai-studio"], "allow_fallbacks": False},
            "reasoning": {"enabled": False},
        },
    },
    # 2026-05-11: Boris explicit ask — z-ai GLM-5 / GLM-5.1.
    # 202k context, marketing page не подтверждает tool calling —
    # узнаем через бенч (если упадёт на A.single_read = no support).
    # Provider routing не форсируем — OR auto-picks; reasoning OFF
    # на всякий случай (Nemotron prior).
    {
        "label": "OR/glm-5",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "z-ai/glm-5",
        "price_in": 0.60, "price_out": 1.92,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    {
        "label": "OR/glm-5.1",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "z-ai/glm-5.1",
        "price_in": 0.98, "price_out": 3.08,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    # 2026-05-12: Boris explicit ask — anthropic/claude-haiku-4.5 via OR.
    # 200k context, tool calling supported. Pricing $1/$5 per 1M
    # (~16x mimo) — кандидат на quality-tier, не cost-tier.
    {
        "label": "OR/claude-haiku-4.5",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "anthropic/claude-haiku-4.5",
        "price_in": 1.0, "price_out": 5.0,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    # 2026-05-12: Boris explicit ask — gpt-4o-mini, gemini-2.5-flash,
    # gemini-2.5-flash-lite via OR. Comparison-tier среди budget-моделей.
    {
        "label": "OR/gpt-4o-mini",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "openai/gpt-4o-mini",
        "price_in": 0.15, "price_out": 0.60,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    {
        "label": "OR/gemini-2.5-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemini-2.5-flash",
        "price_in": 0.30, "price_out": 2.50,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    {
        "label": "OR/gemini-2.5-flash-lite",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "google/gemini-2.5-flash-lite",
        "price_in": 0.10, "price_out": 0.40,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    # 2026-05-12: Boris ask — qwen/qwen3.6-plus. Hybrid linear-attention
    # + sparse MoE. 1M context. $0.325/$1.95 (~4-6x mimo).
    {
        "label": "OR/qwen3.6-plus",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "qwen/qwen3.6-plus",
        "price_in": 0.325, "price_out": 1.95,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    # 2026-05-12: Boris ask — qwen3.5 family. flash (cheap, 1M ctx),
    # 397B-A17B (large MoE, 262k ctx).
    {
        "label": "OR/qwen3.5-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "qwen/qwen3.5-flash-02-23",
        "price_in": 0.065, "price_out": 0.26,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
    {
        "label": "OR/qwen3.5-397b",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": _openrouter_key(),
        "model": "qwen/qwen3.5-397b-a17b",
        "price_in": 0.39, "price_out": 2.34,
        "extra_body": {
            "reasoning": {"enabled": False},
        },
    },
]


# ---------------------------------------------------------------------------
# Stub tools — realistic shapes but no DB, so every provider sees the
# same behaviour. Tool RESULTS hardcoded so the LLM has something to
# consume in the second iteration.
# ---------------------------------------------------------------------------


@tool
def list_shopping() -> str:
    """Return the user's current pending shopping list (title, id, quantity)."""
    return json.dumps(
        [
            {"id": "si_1", "title": "молоко", "quantity_text": "1 л"},
            {"id": "si_2", "title": "хлеб", "quantity_text": None},
            {"id": "si_3", "title": "яйца", "quantity_text": "10 шт"},
        ],
        ensure_ascii=False,
    )


@tool
def list_menu() -> str:
    """Return the planned menu for today (breakfast, lunch, dinner)."""
    return json.dumps(
        {
            "date": "2026-04-22",
            "breakfast": "омлет",
            "lunch": "борщ",
            "dinner": "плов с курицей",
        },
        ensure_ascii=False,
    )


@tool
def search_recipes(query: str = "") -> str:
    """Return all saved recipes in the user's book, optionally filtered by query."""
    return json.dumps(
        [
            {"id": "rec_1", "title": "Борщ"},
            {"id": "rec_2", "title": "Окрошка"},
        ],
        ensure_ascii=False,
    )


@tool
def save_recipes_batch(recipes: list[dict]) -> str:
    """Save a batch of recipes at once. Each recipe must have {title, ingredients, source}."""
    return f"ok:created={len(recipes)}:skipped=0"


# 2026-05-10: дополнительные tool stubs для edge-case сценариев.
# delete_recipe всегда возвращает ошибку not_found — проверяем как
# LLM обрабатывает tool failures (graceful recovery vs crash).
@tool
def delete_recipe(title: str) -> str:
    """Delete a recipe from the user's book by title."""
    return f"error:not_found:{title}"


# log_unsupported_request — реальный production tool. Требуется ДО
# того как LLM скажет user'у «не умею X». Проверяем что модель
# его вызывает а не просто отказывает без логирования.
@tool
def log_unsupported_request(reason: str) -> str:
    """Log a request the assistant cannot fulfil. MUST be called BEFORE telling user 'я не могу'."""
    return f"ok:logged:{reason[:30]}"


# 2026-05-11: schedule_reminder + list_reminders для сценариев G/H.
# Production несколько раз промахнулась на reminder dispatch (Nemotron
# reasoning ON: tool вызван, English greeting в final reply substituted;
# reasoning OFF: tools=[] на 3 turns подряд из-за history poisoning).
# Без покрытия в бенче эти классы багов мы не отлавливаем.
@tool
def schedule_reminder(
    text: str,
    trigger_iso: str | None = None,
    recurrence_rule: str | None = None,
) -> str:
    """Schedule a reminder for the user. text — what to remind, trigger_iso — UTC ISO datetime,
    recurrence_rule — optional RRULE string for repeats."""
    return (
        f"ok:scheduled:reminder_id=rem_test123:"
        f"trigger={trigger_iso}:rrule={recurrence_rule}"
    )


@tool
def list_reminders() -> str:
    """Return the user's pending reminders."""
    return "[]"


TOOLS = [
    list_shopping, list_menu, search_recipes, save_recipes_batch,
    delete_recipe, log_unsupported_request,
    schedule_reminder, list_reminders,
]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


SCENARIOS = [
    {
        "name": "A.single_read",
        "user_text": "что у меня сейчас в списке покупок?",
        "expected_tools": ["list_shopping"],
    },
    {
        "name": "B.multi_read",
        "user_text": "что у меня в списке покупок и какое меню на сегодня?",
        "expected_tools": ["list_shopping", "list_menu"],
    },
    {
        "name": "C.batch_write",
        "user_text": (
            "сохрани пять рецептов с ингредиентами: "
            "борщ, омлет, плов с курицей, котлеты, блины с творогом"
        ),
        "expected_tools": ["save_recipes_batch"],
    },
    # 2026-05-10: edge-case сценарии для оценки soul-директивы под
    # нестандартными нагрузками. Не покрыты A/B/C.
    {
        "name": "D.tool_error",
        "user_text": "удали из книги рецепт борща",
        # Ожидаем: delete_recipe вернёт error → LLM объяснит юзеру + подскажет search_recipes
        "expected_tools": ["delete_recipe"],
    },
    {
        "name": "E.empathy",
        "user_text": "устала ужасно, чувствую себя плохо. Не знаю что приготовить на ужин",
        # Ожидаем: эмоциональный ответ ПЕРВЫМ, потом возможно search_recipes
        # для простого варианта. Не должна спамить сложными tool-вызовами.
        "expected_tools": [],  # tool optional — главное эмпатия
    },
    {
        "name": "F.capability_reject",
        "user_text": "следи за курсом доллара и пиши мне когда вырастет на 5%",
        # Ожидаем: log_unsupported_request → честный отказ. Среда НЕ умеет
        # background polling. Если ответит «готово, настроила» — fail.
        "expected_tools": ["log_unsupported_request"],
    },
    # 2026-05-11: reminder-dispatch сценарии. Production smoke 2026-05-10
    # выявил что Nemotron на «поставь напоминание X» иногда обещает но
    # НЕ вызывает schedule_reminder (наш _TOOL_DISCIPLINE_ADDENDUM явно
    # запрещает обещание без tool — но модель его игнорит). Бенч теперь
    # покрывает 2 разновидности: clean history (G) и poisoned history (H).
    {
        "name": "G.schedule_reminder",
        "user_text": (
            "Поставь пожалуйста на завтра напоминание в 9 часов утра "
            "размяться с гирей."
        ),
        # Ожидаем: schedule_reminder вызван с text + trigger_iso.
        # Reply содержит подтверждение в стиле Среды (на русском, с эмодзи).
        "expected_tools": ["schedule_reminder"],
    },
    {
        "name": "H.poisoned_history",
        "user_text": (
            "Хочу завтра выпить воду в 14:00, поставь напоминание."
        ),
        # Заражённая история: 4 turns где бот пообещал, не вызвал tool.
        # Реальный case с production 2026-05-10 21:23-21:24 (Boris).
        # Модель должна РЕЗИСТИРОВАТЬ паттерну и всё равно вызвать tool.
        "history": [
            ("user",
             "Поставь напоминание на завтра в 10 утра погулять с собакой"),
            ("assistant",
             "Хорошо, поставлю напоминание на завтра в 10:00 — погулять "
             "с собакой. Нужно ли напоминание за сколько-то минут до? 🐕‍🦺"),
            ("user", "Нет, за сколько ты сама обычно напоминаешь?"),
            ("assistant",
             "Уточняю — хочешь напомнить заранее? Например за 10/15/30 мин? "
             "Если не нужно — скажи «без напоминания»."),
        ],
        "expected_tools": ["schedule_reminder"],
    },
]


# 2026-05-10: «Душа Среды» — стержень характера, не косметика.
# 2026-05-10 PM: после triple-review (Claude/Codex/Xiaomi) одна из claims:
# "bench копия SOUL_DIRECTIVE дрейфует от prod" — фикс через единый
# источник истины. Импортируем _SOUL_DIRECTIVE из handlers.py.
#
# Также: build_system_prompt("housewife_assistant") УЖЕ включает
# _SOUL_DIRECTIVE в prod (с 2026-05-10). Поэтому --soul сейчас НЕ имеет
# смысла как «добавить душу к голому промпту» — мы должны наоборот
# уметь УБРАТЬ душу для baseline-сравнения. Семантика флага инвертирована:
#   --soul ON  (default): использовать как есть (с soul, как в проде)
#   --no-soul          : strip soul-блок для apples-to-apples с наследием
from sreda.runtime.handlers import _SOUL_DIRECTIVE  # canonical source of truth

SOUL_DIRECTIVE = _SOUL_DIRECTIVE  # back-compat alias for older code paths

SYSTEM_PROMPT_BASE = build_system_prompt("housewife_assistant")  # already with soul
SYSTEM_PROMPT = SYSTEM_PROMPT_BASE  # default
MAX_ITERS = 4


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_one_turn(provider, scenario):
    if not provider["api_key"]:
        return {"error": "no api_key", "provider": provider["label"]}
    # 2026-05-10: extra_body для OpenRouter provider routing
    # (например forcing DeepInfra/bf16 на nemotron). Опционально.
    extra_body = provider.get("extra_body")
    llm_kwargs = {
        "base_url": provider["base_url"],
        "api_key": provider["api_key"],
        "model": provider["model"],
        "temperature": 0.2,
        "timeout": 60,
    }
    if extra_body:
        llm_kwargs["extra_body"] = extra_body
    llm = ChatOpenAI(**llm_kwargs).bind_tools(TOOLS)

    tools_by_name = {t.name: t for t in TOOLS}
    # 2026-05-11: support optional `history` field in scenario — list of
    # (role, content) tuples that go between system and the actual user_text.
    # Used by H.poisoned_history to simulate the prod situation where prior
    # turns trained the model into a "promise without tool" pattern.
    messages: list = [SystemMessage(content=SYSTEM_PROMPT)]
    for role, content in scenario.get("history", []):
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
        else:
            raise ValueError(f"unknown history role: {role}")
    messages.append(HumanMessage(content=scenario["user_text"]))
    iters = 0
    observed_tools: list[str] = []
    total_in = 0
    total_out = 0
    final_text = ""

    t_start = time.monotonic()
    try:
        for i in range(MAX_ITERS):
            iters += 1
            ai = llm.invoke(messages)
            messages.append(ai)
            usage = getattr(ai, "usage_metadata", None) or {}
            total_in += int(usage.get("input_tokens") or 0)
            total_out += int(usage.get("output_tokens") or 0)
            calls = getattr(ai, "tool_calls", None) or []
            observed_tools.extend(tc.get("name", "?") for tc in calls)
            if not calls:
                final_text = str(ai.content or "")[:200]
                break
            for tc in calls:
                tname = tc.get("name")
                targs = tc.get("args") or {}
                tc_id = tc.get("id", "")
                tool = tools_by_name.get(tname)
                if tool is None:
                    result = f"error:unknown_tool:{tname}"
                else:
                    try:
                        result = tool.invoke(targs)
                    except Exception as e:  # noqa: BLE001
                        result = f"error:{type(e).__name__}:{e}"
                messages.append(ToolMessage(content=str(result), tool_call_id=tc_id))
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "provider": provider["label"],
            "scenario": scenario["name"],
        }
    wall = time.monotonic() - t_start

    return {
        "provider": provider["label"],
        "scenario": scenario["name"],
        "wall_s": round(wall, 2),
        "iters": iters,
        "tools": observed_tools,
        "in_tok": total_in,
        "out_tok": total_out,
        "final_preview": final_text,
    }


def main(runs_per_pair: int = 2, only_labels: list[str] | None = None) -> int:
    # 2026-04-28: support filtering via --only label1,label2,... чтобы
    # тестить отдельные модели без запуска всей матрицы (стоит денег).
    providers = PROVIDERS
    if only_labels:
        wanted = {lbl.strip().lower() for lbl in only_labels if lbl.strip()}
        providers = [
            p for p in PROVIDERS
            if p["label"].lower() in wanted
            or any(w in p["label"].lower() for w in wanted)
        ]
        if not providers:
            print(f"ERROR: --only {only_labels!r} matched no providers.")
            print(f"Available labels: {[p['label'] for p in PROVIDERS]}")
            return 2

    print(f"System-prompt size: {len(SYSTEM_PROMPT)} chars")
    print(f"Providers: {len(providers)}, scenarios: {len(SCENARIOS)}, runs each: {runs_per_pair}")
    if only_labels:
        print(f"Filtered: {[p['label'] for p in providers]}")
    print()

    header = (
        f"{'provider':<24} {'scenario':<16} {'wall (s)':>10} {'iters':>6} "
        f"{'in_tok':>7} {'out_tok':>8} {'cost $':>10} {'tools':<40}"
    )
    print(header)
    print("-" * len(header))
    total_cost_by_provider: dict[str, float] = {}

    agg: dict[tuple[str, str], list[float]] = {}

    for provider in providers:
        # Cerebras qwen-3-235b preview is rate-limited to 5 RPM.
        # Pause between runs so we don't blow the quota and miss all data.
        slow_provider = "qwen-3-235b" in provider["model"]
        for scenario in SCENARIOS:
            walls: list[float] = []
            last = None
            for _ in range(runs_per_pair):
                if slow_provider:
                    time.sleep(15)
                last = _run_one_turn(provider, scenario)
                if last.get("error"):
                    print(f"{provider['label']:<24} {scenario['name']:<16}   ERR   {last['error'][:80]}")
                    break
                walls.append(last["wall_s"])
            if walls and last and "error" not in last:
                median = round(statistics.median(walls), 2)
                agg[(provider["label"], scenario["name"])] = walls
                tools_str = ",".join(last.get("tools", []))[:40]
                # Cost for the LAST run shown (median chosen visually;
                # tokens may drift across runs but order of magnitude
                # identical). Summed per-provider below for a true total.
                cost_one = (
                    last["in_tok"] * provider["price_in"] / 1_000_000
                    + last["out_tok"] * provider["price_out"] / 1_000_000
                )
                total_cost_by_provider[provider["label"]] = (
                    total_cost_by_provider.get(provider["label"], 0.0)
                    + cost_one * len(walls)
                )
                print(
                    f"{provider['label']:<24} {scenario['name']:<16} "
                    f"{median:>10} {last['iters']:>6} {last['in_tok']:>7} "
                    f"{last['out_tok']:>8} {cost_one:>10.5f} {tools_str:<40}"
                )
                fp = last.get("final_preview") or ""
                if fp:
                    print(f"    reply: {fp[:160]!r}")

    print()
    print("---- total spend per provider (all scenarios × all runs) ----")
    grand = 0.0
    for label, cost in sorted(total_cost_by_provider.items(), key=lambda kv: kv[1]):
        grand += cost
        print(f"  {label:<24} ${cost:.5f}")
    print(f"  {'GRAND TOTAL':<24} ${grand:.5f}")
    print()
    print("---- median wall per (provider, scenario) ----")
    header2 = f"{'provider':<24}" + "".join(f" {s['name']:>16}" for s in SCENARIOS)
    print(header2)
    for provider in providers:
        row = f"{provider['label']:<24}"
        for scenario in SCENARIOS:
            walls = agg.get((provider["label"], scenario["name"]))
            if walls:
                row += f" {statistics.median(walls):>16.2f}"
            else:
                row += f" {'ERR':>16}"
        print(row)
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--only", default=None,
        help="CSV labels (case-insensitive substring match) для фильтра. "
             "Пример: --only deepseek,mimo-v2.5-pro",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Сколько повторов на каждую (provider, scenario) пару. Default: 2.",
    )
    parser.add_argument(
        "--soul", action="store_true",
        help="DEPRECATED no-op (2026-05-10): _SOUL_DIRECTIVE теперь часть "
             "production build_system_prompt() из handlers.py — bench по "
             "default тестирует prompt 1-к-1 с проdом. Флаг сохранён для "
             "back-compat запусков.",
    )
    parser.add_argument(
        "--no-soul", action="store_true",
        help="Снять _SOUL_DIRECTIVE из system prompt (для baseline-сравнения "
             "с дософт версией). Полезно для A/B 'с душой vs без'.",
    )
    args = parser.parse_args()
    only_labels = args.only.split(",") if args.only else None
    if args.no_soul:
        SYSTEM_PROMPT = SYSTEM_PROMPT_BASE.replace(_SOUL_DIRECTIVE + "\n", "")
        if SYSTEM_PROMPT == SYSTEM_PROMPT_BASE:
            # Не нашли с trailing \n — пробуем без
            SYSTEM_PROMPT = SYSTEM_PROMPT_BASE.replace(_SOUL_DIRECTIVE, "")
        print(f"[no-soul] SYSTEM_PROMPT shrunk by "
              f"{len(SYSTEM_PROMPT_BASE) - len(SYSTEM_PROMPT)} chars")
    elif args.soul:
        print("[soul] flag is a no-op (soul already in production prompt). "
              "Use --no-soul to strip it.")
    raise SystemExit(main(runs_per_pair=args.runs, only_labels=only_labels))
