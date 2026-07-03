"""ToolSpec instances for the MEMORY family (Sub-A4 phase 9).

3 tools migrated: save_core_fact, save_episode, recall_memory.

Sources of truth:
- Tool signatures: ``runtime/tools.py:89-224`` (cross-skill tools,
  not housewife-specific — also available to other skills)
- Output schemas + parsers: ``services/tool_schemas/housewife.py``
  — SaveCoreFactOk, SaveEpisodeOk, RecallMemoryOk + RecallMemoryHit
- Repository: ``db/repositories/memory.py:MemoryRepository``
  — embedding via bge-m3, cosine-similarity recall
- ``recall_memory`` searches THREE stores: core+episodic memory,
  active checklist items, pending reminders
  (runtime/tools.py:168-176 recall-broadcast, 2026-05-04)
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sreda.services.tool_schemas.base import ToolSpec
from sreda.services.tool_schemas.housewife import (
    RecallMemoryOutput,
    SaveCoreFactOutput,
    SaveEpisodeOutput,
)


# ---------------------------------------------------------------------------
# Memory-specific aliases
# ---------------------------------------------------------------------------


CoreFactContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
"""Stable long-term fact about user — family, work, location,
long-term preferences. 1000 char cap is generous for multi-fact
sentences; runtime stores via EncryptedString without further
truncation. Embedding via bge-m3 (1024-dim)."""


EpisodeSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Short-term event/conversation summary (1-2 sentences).
Episodic memories are subject to retention cleanup over time
(unlike core facts which are stable). 500 char cap matches typical
2-sentence summary length."""


RecallQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
"""Search query for recall_memory. Best practices per
runtime/tools.py:189-192: use user's exact phrasing OR specific
keywords («ткани характеристики», «дети возраст», «адреса»).
500 char cap covers typical query lengths."""


CategoryName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        # R1 (Codex high): контрол-символы (\r\n\t\0 и прочие C0) ломают построчный контракт
        # вывода `created:<id>:<name>` → отсекаем на входе. strip_whitespace снимает хвостовые.
        pattern=r"^[^\x00-\x1f]+$",
    ),
]
"""#262b: имя пользовательской категории памяти. 100 — как лимит мини-аппа
(DB String(120), запас). Нормализация/уникальность — в repo (normalize_for_dedup);
«Общее» зарезервировано за системной Common. Контрол-символы запрещены (R1)."""


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class SaveCoreFactInput(BaseModel):
    """Save a durable truth about the user (core tier).

    Use ONLY for stable facts that won't change next week —
    family composition, work, location, long-term preferences.
    NOT for moods, transient events, or shifting opinions."""

    model_config = ConfigDict(extra="forbid")
    content: CoreFactContent
    # #262b: опц. имя категории. Задано → факт кладётся в неё (создаётся, если нет); иначе в «Общее».
    category: CategoryName | None = None


class SaveEpisodeInput(BaseModel):
    """Save a recent event/state summary (episodic tier).

    Use for «what's been happening lately» context — recent
    events, feelings, situations. Subject to retention cleanup
    (unlike core facts). If the thing is actually durable,
    use ``save_core_fact`` instead."""

    model_config = ConfigDict(extra="forbid")
    summary: EpisodeSummary


class RecallMemoryInput(BaseModel):
    """Recall-broadcast search across memory + active checklists +
    pending reminders.

    Per runtime/tools.py:158-187 — ALWAYS call before claiming
    «у меня нет данных» / «я этого не помню». ``top_k`` defaults
    to 3, max 10 (runtime clamps internally)."""

    model_config = ConfigDict(extra="forbid")
    query: RecallQuery
    top_k: int = Field(default=3, ge=1, le=10)


# ---------------------------------------------------------------------------
# ToolSpec instances
# ---------------------------------------------------------------------------


SAVE_CORE_FACT_SPEC = ToolSpec(
    name="save_core_fact",
    # #128: ужато; контракт «не дедуплицирует при записи» сохранён
    # #213 Срез A: описание ужато (headroom-гейт префикса; дедуп-правило
    # остаётся в mutex_notes, полная семантика category — в _REACT_TOOL_DESC
    # react_loop; старый plan-execute путь заморожен).
    description=(
        "Сохранить СТАБИЛЬНЫЙ долгосрочный факт о юзере в core "
        "memory (семья, работа, проживание, долгосрочные "
        "предпочтения) — то, что будет верно и через год. Настроение "
        "и transient-события → save_episode. category (опц.) — только "
        "если юзер ЯВНО назвал категорию; иначе не передавай. "
        "Возвращает saved_core:<memory_id>."
    ),
    family="memory",
    effect="write",
    read_domains=[],
    write_domains=["memory"],
    input_model=SaveCoreFactInput,
    output_model=SaveCoreFactOutput,
    trigger_examples=[
        "у меня двое детей: Маша 7 лет и Петя 4",
        "запомни что я работаю в банке",
        "живу в Москве, центр",
        "у мужа аллергия на орехи",
    ],
    mutex_notes=[
        "ТОЛЬКО для стабильных фактов. Для recent events / mood → save_episode.",
        "Runtime НЕ дедупит save-time — каждый вызов = новая строка. НЕ вызывай повторно по тому же факту.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


# #262b create_memory_category: спека НЕТ НАМЕРЕННО — инструмент ReAct-only
# (families.REACT_ONLY_TOOLS, канон #210: старый plan-execute путь заморожен,
# spec-обвязка для него не строится). Долг Среза 1 #270 закрыт в #213 Срезе A:
# полный комплект спек+парсер раздувал prod-like префикс планировщика за
# headroom-гейт и требовал презентер для мёртвого пути.


SAVE_EPISODE_SPEC = ToolSpec(
    name="save_episode",
    description=(
        "Сохранить КРАТКОСРОЧНЫЙ episode/состояние в episodic memory "
        "(recent events, feelings, context «что было на этой неделе»). "
        "Subject to retention cleanup — episodic уйдёт через какое-то "
        "время, не для долгосрочных фактов (для них → save_core_fact). "
        "1-2 предложения. Возвращает saved_episode:<memory_id>."
    ),
    family="memory",
    effect="write",
    read_domains=[],
    write_domains=["memory"],
    input_model=SaveEpisodeInput,
    output_model=SaveEpisodeOutput,
    trigger_examples=[
        "сегодня устала, поздно легла",
        "вчера спорили с мужем из-за выходных",
        "на этой неделе много работы",
        "ребёнок плохо спал ночью",
    ],
    mutex_notes=[
        "ТОЛЬКО для recent state / events. Для долгосрочных фактов → save_core_fact.",
        "Subject to retention — episodic не вечный, не клади сюда что хочешь помнить навсегда.",
    ],
    timeout_seconds=15,
    side_effect_class="transactional_write",
)


RECALL_MEMORY_SPEC = ToolSpec(
    name="recall_memory",
    # #128: ужато; правило «сначала проверь, потом „не помню“» сохранено.
    # Возвраты после ревью среза 1: «что было в переписке» (единственная
    # лексическая подсказка жанра — trigger_examples в промпт НЕ рендерятся,
    # находка субагента) и metadata в составе hits (Codex medium).
    description=(
        "Найти данные по трём хранилищам: core/episodic memory + "
        "active checklist items + pending reminders. ВСЕГДА вызывай "
        "ПЕРЕД ответом «нет данных»/«не помню», и когда юзер просит "
        "перечисление («покажи все X», «что у меня про Y», «что было в "
        "переписке про Z»). Возвращает hits: content + source "
        "(memory:core/memory:episodic/checklist:<id>/reminder:<id>) + "
        "score + metadata. Источник юзеру называй по-русски («у тебя в "
        "чек-листе»), не техжаргоном."
    ),
    family="memory",
    effect="read",
    read_domains=["memory", "checklists", "reminders"],
    write_domains=[],
    input_model=RecallMemoryInput,
    output_model=RecallMemoryOutput,
    trigger_examples=[
        "помнишь я про детей рассказывала",
        "что у меня про работу было",
        "покажи все мои аллергии",
        "что я планировала на выходные",
    ],
    mutex_notes=[
        "ВСЕГДА перед claim'ом отсутствия данных. Source указывай по смыслу для юзера: чек-лист / напоминание / память.",
        "top_k=3 default; 10 для list-style запросов «покажи все X».",
    ],
    timeout_seconds=10,
    side_effect_class="read_only",
)


MEMORY_SPECS: list[ToolSpec] = [
    SAVE_CORE_FACT_SPEC,
    SAVE_EPISODE_SPEC,
    RECALL_MEMORY_SPEC,
]


__all__ = [
    "CoreFactContent",
    "EpisodeSummary",
    "MEMORY_SPECS",
    "RECALL_MEMORY_SPEC",
    "RecallMemoryInput",
    "RecallQuery",
    "SAVE_CORE_FACT_SPEC",
    "SAVE_EPISODE_SPEC",
    "SaveCoreFactInput",
    "SaveEpisodeInput",
]
