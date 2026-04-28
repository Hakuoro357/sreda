"""Onboarding flow for Помощник домохозяйки.

State machine kept in ``TenantUserSkillConfig.skill_params_json`` under
the ``onboarding`` key — no new tables or migrations. The chat handler
reads the state each turn, injects an instruction block into the system
prompt, and LLM-tools (``onboarding_answered`` / ``onboarding_deferred``)
mutate it as the conversation progresses.

Flow:
  subscribe → init(status=not_started) + schedule kickoff job 5 min out
  user writes first  → start() from handler path
  user silent 5 min  → start() from worker path + send intro
  each turn          → prompt tells LLM the current topic, it asks,
                       user answers, LLM calls mark_answered/deferred
  all topics closed  → status=complete, LLM sends a closing message

Two-pass skip policy:
  First "потом / пропусти" on a topic → state=skipped_once; we move on
  but the topic comes back at the end of the list.
  Second refusal on the same topic → state=skipped (permanent).
  This way we don't badger, but we don't give up on the first "не сейчас"
  either — the user might be busy with a specific question at that
  moment.

Depth cap:
  LLM can follow up on the same topic at most 2 times (``current_topic_depth``
  0 → 1 → 2; at 2 the prompt tells it to wrap up). Prevents the flow
  getting stuck on a single question.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from sreda.db.repositories.user_profile import UserProfileRepository

HOUSEWIFE_FEATURE_KEY = "housewife_assistant"

logger = logging.getLogger(__name__)


# 2026-04-27 (вечер): TOPIC_ORDER сокращена до одной темы `addressing`.
# Раньше было 6 тем (addressing, self_intro, family, diet, routine,
# pain_point) — это создавало ощущение анкеты после длинной pending-
# цепочки. Решение: после approve LLM спрашивает только имя, дальше
# обычный chat-flow без проактивных расспросов. Возврат остальных тем
# — backlog (`docs/tomorrow-plan.md` пункт 8).
#
# Константы остальных топиков сохраняются как valid string identifiers
# (могут использоваться через update_profile_field в свободной форме),
# но НЕ участвуют в активном цикле сбора.
TOPIC_ADDRESSING = "addressing"
TOPIC_SELF_INTRO = "self_intro"
TOPIC_FAMILY = "family"
TOPIC_DIET = "diet"
TOPIC_ROUTINE = "routine"
TOPIC_PAIN_POINT = "pain_point"

TOPIC_ORDER: tuple[str, ...] = (
    TOPIC_ADDRESSING,
)

# Prompt-facing descriptions. The LLM formulates the actual question;
# these are just seeds so it knows what the topic is about. Описания
# удалённых из TOPIC_ORDER тем оставлены для совместимости — на случай
# если LLM передаст один из них в `onboarding_answered(topic=...)`.
TOPIC_DESCRIPTIONS: dict[str, str] = {
    TOPIC_ADDRESSING: "Как обращаться к пользователю — имя или прозвище.",
    TOPIC_SELF_INTRO: "Короткий рассказ о себе — чем занимается, что любит.",
    TOPIC_FAMILY: "Кто живёт вместе (супруг/а, дети с именами и возрастами, питомцы).",
    TOPIC_DIET: "Ограничения по питанию — что не любит, что нельзя.",
    TOPIC_ROUTINE: "Ключевые якоря дня — подъём, работа/садик/школа, отбой.",
    TOPIC_PAIN_POINT: "С чем чаще всего хочется помощи — меню / покупки / расписание детей / уборка / дневник дел.",
}

STATE_PENDING = "pending"
STATE_ANSWERED = "answered"
STATE_SKIPPED_ONCE = "skipped_once"
STATE_SKIPPED = "skipped"

STATUS_NOT_STARTED = "not_started"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETE = "complete"
STATUS_ABANDONED = "abandoned"

_MAX_TOPIC_DEPTH = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _default_state() -> dict[str, Any]:
    """A fresh ``onboarding`` dict for a user who just subscribed."""
    return {
        "status": STATUS_NOT_STARTED,
        "started_at": None,
        "completed_at": None,
        "kickoff_scheduled_at": None,
        "topics": {
            key: {"state": STATE_PENDING, "answer": None}
            for key in TOPIC_ORDER
        },
        "current_topic": None,
        "current_topic_depth": 0,
    }


@dataclass(slots=True)
class OnboardingState:
    """Lightweight view over the raw dict — for callers that want a
    typed object. Most of the module operates on the dict directly."""

    status: str
    current_topic: str | None
    current_topic_depth: int
    topics: dict[str, dict[str, Any]]
    started_at: str | None
    completed_at: str | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OnboardingState":
        return cls(
            status=raw.get("status", STATUS_NOT_STARTED),
            current_topic=raw.get("current_topic"),
            current_topic_depth=int(raw.get("current_topic_depth", 0)),
            topics=raw.get("topics") or {},
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
        )


def _next_topic(topics: dict[str, dict[str, Any]]) -> str | None:
    """Return the next topic to ask about, or None if all done.

    Two-pass: first pass for ``pending``, second pass gives ``skipped_once``
    topics another chance at the end of the list. ``answered`` and
    ``skipped`` (permanent) are filtered out.
    """
    for key in TOPIC_ORDER:
        t = topics.get(key) or {}
        if t.get("state") == STATE_PENDING:
            return key
    for key in TOPIC_ORDER:
        t = topics.get(key) or {}
        if t.get("state") == STATE_SKIPPED_ONCE:
            return key
    return None


class HousewifeOnboardingService:
    """Service facade over the skill_params onboarding state.

    Callers: the chat handler (read state each turn), the LLM-tool
    closures in ``housewife_chat_tools`` (mutate), the post-subscription
    hook + kickoff worker.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = UserProfileRepository(session)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_raw_state(
        self, *, tenant_id: str, user_id: str | None
    ) -> dict[str, Any]:
        """Return the raw onboarding dict, merged with defaults for
        any missing fields (forward-compatible when we add fields)."""
        if not user_id:
            return _default_state()
        config = self.repo.get_skill_config(
            tenant_id, user_id, HOUSEWIFE_FEATURE_KEY
        )
        params = (
            self.repo.decode_skill_params(config) if config is not None else {}
        )
        state = params.get("onboarding")
        if not isinstance(state, dict):
            return _default_state()
        return _merge_with_defaults(state)

    def get_state(
        self, *, tenant_id: str, user_id: str | None
    ) -> OnboardingState:
        return OnboardingState.from_dict(
            self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def initialize(
        self, *, tenant_id: str, user_id: str, source: str = "system"
    ) -> dict[str, Any]:
        """Create a fresh onboarding record after subscription. Idempotent:
        if one is already present, returns it untouched (never reset a
        user mid-flow just because they re-subscribed)."""
        existing = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        if existing.get("status") != STATUS_NOT_STARTED or existing.get("started_at"):
            # Either in progress, complete, or already initialized.
            return existing
        fresh = _default_state()
        self._persist(
            tenant_id=tenant_id, user_id=user_id, state=fresh, source=source
        )
        return fresh

    def schedule_kickoff(
        self,
        *,
        tenant_id: str,
        user_id: str,
        delay_minutes: int = 5,
        source: str = "system",
    ) -> dict[str, Any]:
        """Initialize (if needed) and stamp ``kickoff_scheduled_at``.

        If the user doesn't write first within ``delay_minutes``, the
        kickoff worker picks this up and fires the intro. If the user
        DOES write first, the chat handler flips status to in_progress
        before the scheduled time and the worker's filter skips it.

        Idempotent: if onboarding is already started/complete, returns
        state untouched (no resetting).
        """
        state = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        if state.get("status") in (STATUS_IN_PROGRESS, STATUS_COMPLETE, STATUS_ABANDONED):
            return state
        if state.get("status") != STATUS_NOT_STARTED:
            return state  # future states
        if not state.get("kickoff_scheduled_at"):
            scheduled = _utcnow() + timedelta(minutes=delay_minutes)
            state["kickoff_scheduled_at"] = scheduled.isoformat()
            self._persist(
                tenant_id=tenant_id,
                user_id=user_id,
                state=state,
                source=source,
            )
        return state

    def start(
        self, *, tenant_id: str, user_id: str, source: str = "system"
    ) -> dict[str, Any]:
        """Transition not_started → in_progress. Sets ``current_topic``
        to the first pending topic (``addressing``)."""
        state = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        if state.get("status") == STATUS_IN_PROGRESS:
            return state
        if state.get("status") in (STATUS_COMPLETE, STATUS_ABANDONED):
            return state
        state["status"] = STATUS_IN_PROGRESS
        state["started_at"] = state.get("started_at") or _utcnow_iso()
        state["current_topic"] = _next_topic(state["topics"])
        state["current_topic_depth"] = 0
        self._persist(
            tenant_id=tenant_id, user_id=user_id, state=state, source=source
        )
        return state

    def mark_answered(
        self,
        *,
        tenant_id: str,
        user_id: str,
        topic: str,
        summary: str,
    ) -> dict[str, Any]:
        """Record an answer for ``topic`` and advance ``current_topic``.

        Also mirrors ``addressing`` into ``TenantUserProfile.display_name``
        so downstream prompts can use the user's preferred name without
        digging into the skill_params blob.
        """
        if topic not in TOPIC_DESCRIPTIONS:
            raise ValueError(f"unknown topic: {topic!r}")
        state = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        topic_row = state["topics"].get(topic) or {}
        topic_row["state"] = STATE_ANSWERED
        topic_row["answer"] = (summary or "").strip()[:500] or None
        state["topics"][topic] = topic_row

        # Side effect: addressing answer is the user's name — persist
        # into the structured profile column, not just the skill_params
        # blob, so chat and reminders pick it up naturally.
        if topic == TOPIC_ADDRESSING and topic_row["answer"]:
            self._set_display_name(
                tenant_id=tenant_id, user_id=user_id, name=topic_row["answer"]
            )

        self._advance_current_topic(state)
        self._persist(
            tenant_id=tenant_id,
            user_id=user_id,
            state=state,
            source="agent_tool_direct",
        )
        return state

    def mark_deferred(
        self,
        *,
        tenant_id: str,
        user_id: str,
        topic: str,
    ) -> dict[str, Any]:
        """Record a skip for ``topic``. First skip → ``skipped_once``
        (topic comes back at end of list). Second → ``skipped`` (done)."""
        if topic not in TOPIC_DESCRIPTIONS:
            raise ValueError(f"unknown topic: {topic!r}")
        state = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        topic_row = state["topics"].get(topic) or {}
        current = topic_row.get("state")
        if current in (STATE_ANSWERED, STATE_SKIPPED):
            # Already settled. No-op.
            return state
        topic_row["state"] = (
            STATE_SKIPPED if current == STATE_SKIPPED_ONCE else STATE_SKIPPED_ONCE
        )
        state["topics"][topic] = topic_row
        self._advance_current_topic(state)
        self._persist(
            tenant_id=tenant_id,
            user_id=user_id,
            state=state,
            source="agent_tool_direct",
        )
        return state

    def record_follow_up(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        """Increment the per-topic follow-up depth counter. Called by the
        chat handler when the LLM produces another turn on the same
        topic without calling mark_answered/deferred. Stops silently at
        the cap; the prompt is what actually forces the LLM to wrap up."""
        state = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        if state.get("status") != STATUS_IN_PROGRESS:
            return state
        depth = int(state.get("current_topic_depth") or 0) + 1
        state["current_topic_depth"] = min(depth, _MAX_TOPIC_DEPTH)
        self._persist(
            tenant_id=tenant_id,
            user_id=user_id,
            state=state,
            source="agent_tool_direct",
        )
        return state

    def mark_complete(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any]:
        """Explicit completion. Normally set automatically via
        ``_advance_current_topic`` when no topic is left."""
        state = self.get_raw_state(tenant_id=tenant_id, user_id=user_id)
        state["status"] = STATUS_COMPLETE
        state["completed_at"] = state.get("completed_at") or _utcnow_iso()
        state["current_topic"] = None
        state["current_topic_depth"] = 0
        self._persist(
            tenant_id=tenant_id,
            user_id=user_id,
            state=state,
            source="agent_tool_direct",
        )
        return state

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def format_for_prompt(self, state: dict[str, Any]) -> str:
        """Render the [ОНБОРДИНГ] block injected into the system prompt
        when ``status == in_progress``."""
        topics = state.get("topics") or {}
        lines: list[str] = []

        lines.append(
            "Режим первичного знакомства. Мягко собираешь первичный срез "
            "о пользователе по списку тем. Задавай ПО ОДНОМУ вопросу за раз."
        )
        lines.append("")
        lines.append("Статус тем:")
        for key in TOPIC_ORDER:
            t = topics.get(key) or {}
            marker = {
                STATE_ANSWERED: "✅",
                STATE_SKIPPED_ONCE: "⏭  пропустил один раз — спроси ещё раз потом",
                STATE_SKIPPED: "⏹  окончательно пропустил",
                STATE_PENDING: "⏸",
            }.get(t.get("state") or STATE_PENDING, "⏸")
            descr = TOPIC_DESCRIPTIONS[key]
            if t.get("state") == STATE_ANSWERED and t.get("answer"):
                descr = f"{descr} → запомнено: «{t['answer']}»"
            lines.append(f"  - {key}: {marker} — {descr}")

        current = state.get("current_topic")
        depth = int(state.get("current_topic_depth") or 0)
        lines.append("")
        if current is None:
            lines.append(
                "Все темы пройдены. Сразу вызови ``onboarding_complete`` и напиши "
                "короткое завершающее сообщение — поблагодари, спроси что хочет "
                "сделать первым."
            )
        else:
            cap_note = (
                "depth_used=0 из 2 (можно задать уточняющий вопрос если ответ общий)"
                if depth == 0
                else (
                    f"depth_used={depth} из 2 — ещё один уточняющий вопрос максимум"
                    if depth == 1
                    else (
                        "depth_used=2 из 2 — БОЛЬШЕ НЕ УГЛУБЛЯЙСЯ. "
                        "Вызови ``onboarding_answered`` с тем что уже есть, "
                        "или ``onboarding_deferred`` если пользователь молчит."
                    )
                )
            )
            lines.append(f"Текущая тема: **{current}** — {TOPIC_DESCRIPTIONS[current]}")
            lines.append(cap_note)

        lines.append("")
        lines.append("Правила онбординга:")
        lines.append(
            "1. Всегда мягко упомяни возможность пропустить: «можем вернуться "
            "позже, если сейчас не до этого»."
        )
        lines.append(
            "2. Когда пользователь ответил содержательно — вызови "
            "``onboarding_answered(topic='" + (current or "?") + "', summary='...')`` "
            "с коротким 1-2 предложенческим резюме ответа."
        )
        lines.append(
            "3. Если пользователь сказал «потом / не сейчас / пропусти» — "
            "``onboarding_deferred(topic='" + (current or "?") + "', reason='...')``. "
            "НЕ дави, не уговаривай."
        )
        lines.append(
            "4. Если пользователь спросил что-то своё (погоду, напоминание) — "
            "ответь / выполни, и только потом аккуратно верни к теме онбординга."
        )
        lines.append(
            "5. Для темы ``addressing`` summary = ТОЛЬКО имя/ник 1-3 словами. "
            "ПРАВИЛЬНО: summary='Борис', summary='Анна Викторовна', summary='Шеф'. "
            "ЗАПРЕЩЕНО: summary='Пользователя зовут Борис.', "
            "summary='Меня зовут Анна', summary='Пользователь хочет, чтобы его называли «Шеф»'. "
            "Без префиксов, без точки в конце, без кавычек, без пояснений."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_in_progress(self, *, tenant_id: str, user_id: str | None) -> bool:
        return (
            self.get_raw_state(tenant_id=tenant_id, user_id=user_id).get("status")
            == STATUS_IN_PROGRESS
        )

    def is_not_started(self, *, tenant_id: str, user_id: str | None) -> bool:
        return (
            self.get_raw_state(tenant_id=tenant_id, user_id=user_id).get("status")
            == STATUS_NOT_STARTED
        )

    def current_topic(self, *, tenant_id: str, user_id: str | None) -> str | None:
        return self.get_raw_state(
            tenant_id=tenant_id, user_id=user_id
        ).get("current_topic")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _advance_current_topic(self, state: dict[str, Any]) -> None:
        """Pick next topic; if none — flip status=complete."""
        topics = state.get("topics") or {}
        next_key = _next_topic(topics)
        state["current_topic"] = next_key
        state["current_topic_depth"] = 0
        if next_key is None:
            state["status"] = STATUS_COMPLETE
            state["completed_at"] = _utcnow_iso()

    def _persist(
        self,
        *,
        tenant_id: str,
        user_id: str,
        state: dict[str, Any],
        source: str,
    ) -> None:
        """Write the full skill_params back. ``upsert_skill_config``
        replaces ``skill_params_json`` wholesale, so we merge with any
        existing non-onboarding keys first."""
        existing_config = self.repo.get_skill_config(
            tenant_id, user_id, HOUSEWIFE_FEATURE_KEY
        )
        existing_params = (
            self.repo.decode_skill_params(existing_config)
            if existing_config is not None
            else {}
        )
        existing_params["onboarding"] = state
        # source must be one of UPDATE_SOURCES (see db/models/user_profile.py).
        # Callers pass values from that set directly — no mapping.
        self.repo.upsert_skill_config(
            tenant_id,
            user_id,
            HOUSEWIFE_FEATURE_KEY,
            source=source,
            skill_params=existing_params,
        )

    def _set_display_name(
        self, *, tenant_id: str, user_id: str, name: str
    ) -> None:
        """Mirror addressing answer to TenantUserProfile.display_name.

        2026-04-28: вызываем ``_extract_short_name`` чтобы вырезать
        префиксы «Пользователя зовут …», «Меня зовут …», «Пользователь
        хочет, чтобы его называли «X»» и подобный мусор, который LLM
        иногда передаёт как ``summary``. Без этой защиты в проде
        наблюдалось ``display_name = "Пользователя зовут Борис."``.
        Best-effort: если запись профиля ещё не создана,
        ``update_profile`` сама её апсёртит через ``get_or_create_profile``.
        """
        clean = _extract_short_name(name)
        if not clean:
            # Совсем пустой/невнятный summary — не записываем мусор поверх
            # существующего имени.
            return
        try:
            self.repo.update_profile(
                tenant_id,
                user_id,
                source="agent_tool_direct",
                display_name=clean[:120],
            )
        except Exception:  # noqa: BLE001 — naming is sugar, don't kill the turn
            logger.exception(
                "failed to mirror addressing answer into profile.display_name"
            )


# ---------------------------------------------------------------------------
# Display-name sanitizer (2026-04-28 incident response)
# ---------------------------------------------------------------------------
#
# LLM-tool ``onboarding_answered(topic="addressing", summary=...)`` мирорит
# ``summary`` в ``TenantUserProfile.display_name``. Docstring явно говорит
# «just the name», но иногда LLM сохраняет полную фразу:
#   * «Пользователя зовут Борис.»
#   * «Пользователь хочет, чтобы его называли «Шеф».»
#   * «Меня зовут Анна Викторовна»
# В этих случаях имя в админке выглядит как предложение, а в LLM-prompt'е
# секция профиля становится `Имя: Пользователя зовут Борис.`. Helper
# ниже срезает префиксы, кавычки, терминальную пунктуацию и обрезает
# хвост после тире/запятой/точки. Применяется в ``_set_display_name``
# и в ``_validate_proposed_field`` (handlers.py) — две точки входа,
# через которые имя попадает в профиль.

_NAME_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    # «Пользователь хочет, чтобы его называли …» / «Пользователя хочет, чтобы её называли …»
    re.compile(r"^пользовател[ьяй]\s+хочет.*?чтобы\s+(?:его|её|ее)\s+называли\s+", re.IGNORECASE),
    # «Пользователя зовут …», «Пользователь зовут …»
    re.compile(r"^пользовател[ьяй]\s+зовут\s+", re.IGNORECASE),
    # «Меня зовут …»
    re.compile(r"^меня\s+зовут\s+", re.IGNORECASE),
    # «Зови меня …»
    re.compile(r"^зови\s+меня\s+", re.IGNORECASE),
    # «Имя: …», «Имя — …»
    re.compile(r"^имя\s*[:—-]\s*", re.IGNORECASE),
    # «Зовут …» (без «меня/пользователя»)
    re.compile(r"^зовут\s+", re.IGNORECASE),
)

# Окружающая пунктуация (кавычки, скобки, точки, запятые, дефисы), которую
# нужно убрать с краёв. Включает и обычные ASCII, и русские «ёлочки».
_TRIM_CHARS = " .,;:!?'\"«»()[]{}—–-"


def _extract_short_name(text: str | None) -> str:
    """Извлечь короткое имя из произвольной строки от LLM.

    Шаги:
    1. Strip + пустую строку → "".
    2. Срезать известный префикс («Пользователя зовут», «Меня зовут», …).
    3. Убрать окружающие кавычки/пунктуацию.
    4. Если результат содержит пояснительный clause через «—»/«,»/«.»
       (например «Повелитель — просил называть его так»), оставить только
       первую часть.
    5. Обрезать до 30 символов как защиту от мусора.
    """
    if not text:
        return ""
    s = text.strip()
    if not s:
        return ""

    # 1. Срезаем известные префиксы.
    for pattern in _NAME_PREFIX_PATTERNS:
        new = pattern.sub("", s, count=1)
        if new != s:
            s = new
            break

    # 2. Trim окружающую пунктуацию.
    s = s.strip(_TRIM_CHARS)

    # 3. Если есть «— пояснение» / «, пояснение» / «. вторая фраза» —
    #    берём только первую часть. Имя редко содержит такие разделители,
    #    «John — friend» / «Шеф, как обычно» / «Борис. Зови так» — это
    #    мусор на хвосте.
    for sep in ("—", "–", ",", ".", ";", "  "):
        if sep in s:
            s = s.split(sep, 1)[0].strip()

    # 4. Финальный trim после split.
    s = s.strip(_TRIM_CHARS)

    # 5. Cap на 30 символов — защита от случаев когда не удалось ничего
    #    вырезать и осталась длинная фраза.
    return s[:30]


# ---------------------------------------------------------------------------
# Welcome v2 broadcast tour — progress tracking (2026-04-28)
# ---------------------------------------------------------------------------
#
# Когда existing approved тенанты получают рассылку pending-цепочки
# (intro → 10 шагов → done), мы хотим знать кто докуда дошёл, без
# миграций. Решение: пишем прогресс в TenantUserSkillConfig.skill_params_json
# под ключом ``welcome_v2_progress`` с полями started_at / last_branch /
# last_at / completed_at. Админка читает это и показывает emoji-индикатор
# рядом с telegram_id.

WELCOME_V2_PROGRESS_KEY = "welcome_v2_progress"


def record_pb_tour_progress(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    branch: str,
) -> None:
    """Обновить прогресс прохождения welcome v2 тура для (tenant, user).

    Каждый ``pb:<branch>`` callback от approved юзера во время broadcast-
    рассылки вызывает этот хелпер. Поведение:
    * Первый вызов — пишет started_at = NOW.
    * Каждый вызов — обновляет last_branch + last_at.
    * При branch == "done" — пишет completed_at.

    Идемпотентно: повторный вызов с тем же branch просто обновляет
    last_at. Никогда не сбрасывает started_at — юзер не «начинает
    заново», даже если кликнул intro дважды.

    Не валидирует branch против ``pending_bot._BRANCHES`` — это
    защитный слой. Ничего страшного, если в БД попадёт неизвестный
    branch (например, alias `pb:welcome`); при чтении админка просто
    покажет «in_progress».
    """
    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, HOUSEWIFE_FEATURE_KEY)
    params = (
        UserProfileRepository.decode_skill_params(config) if config else {}
    )
    progress = params.get(WELCOME_V2_PROGRESS_KEY) or {}
    if not isinstance(progress, dict):
        progress = {}

    now = datetime.now(timezone.utc).isoformat()
    if not progress.get("started_at"):
        progress["started_at"] = now
    progress["last_branch"] = branch
    progress["last_at"] = now
    if branch == "done":
        progress["completed_at"] = now

    params[WELCOME_V2_PROGRESS_KEY] = progress
    repo.upsert_skill_config(
        tenant_id,
        user_id,
        HOUSEWIFE_FEATURE_KEY,
        source="agent_tool_direct",
        skill_params=params,
    )


def _merge_with_defaults(state: dict[str, Any]) -> dict[str, Any]:
    """Fill in any fields missing from an older state shape."""
    base = _default_state()
    out = dict(base)
    out.update(state)
    topics = dict(base["topics"])
    for key, value in (state.get("topics") or {}).items():
        if key in topics and isinstance(value, dict):
            merged = dict(topics[key])
            merged.update(value)
            topics[key] = merged
    out["topics"] = topics
    return out
