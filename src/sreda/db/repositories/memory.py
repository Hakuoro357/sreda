"""Memory repository (Phase 3).

CRUD + cosine-similarity recall over ``AssistantMemory`` rows. Keeps the
JSON encoding of embeddings in one place so callers deal in plain
``list[float]`` vectors.

At MVP scale (up to a few hundred memories per user) we do the cosine
in Python — no vector index needed. When that stops being fast enough,
swap this file to use pgvector (indexed ``<->`` / ``<=>`` operator) and
the rest of the app stays unchanged.

Recall-broadcast (2026-05-04): also fans out to ``checklist_items`` и
``family_reminders``. Каждый source обёрнут в try/except (R1#8 from
qwen review), pairwise dedup при cosine>0.95 (R1#9), и
``min_per_source=1`` guarantee (R2#1) — каждый source с hits ≥ min_score
включается в final независимо от global rank.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.checklists import Checklist, ChecklistItem
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.memory import AssistantMemory, MemoryCategory
from sreda.services.embeddings import cosine_similarity
from sreda.services.text_normalization import normalize_for_dedup

logger = logging.getLogger(__name__)


MEMORY_TIERS = frozenset({"core", "episodic"})
MEMORY_SOURCES = frozenset({"user_direct", "agent_inferred", "system"})

# #262: системная категория «Common» — дом по умолчанию, неизменяема.
COMMON_SLUG = "common"
COMMON_DISPLAY_NAME = "Общее"
# Зарезервированная норма имени Common (= normalize_for_dedup("Общее") = "общий"). Считаем один раз.
# create_category/rename_category отвергают это имя ВСЕГДА (даже до создания Common) — иначе юзер «украл» бы
# имя и последующий ensure_common упал бы на uq_memory_categories_name (R2 дыра порядка). Миграция 20260630_0071
# держит замороженный литерал + дрейф-тест (не импортит эту константу — историческая стабильность).
COMMON_NAME_NORMALIZED = normalize_for_dedup(COMMON_DISPLAY_NAME)


class MemoryCategoryError(Exception):
    """База для ошибок операций над категориями (API-слой маппит в 4xx)."""


class CategoryNameConflict(MemoryCategoryError):
    """Имя категории дублирует существующее (по name_normalized) → 409."""


class CategoryNotFound(MemoryCategoryError):
    """Категория не существует у этого (tenant,user) → 404 (напр. явный category_id в save())."""


class CategoryImmutable(MemoryCategoryError):
    """Попытка переименовать/удалить системную Common → 409."""


class CategoryConfirmMismatch(MemoryCategoryError):
    """confirm_count не совпал с фактическим числом фактов (набор изменился) → 409."""

    def __init__(self, expected: int, given: int) -> None:
        self.expected = expected
        self.given = given
        super().__init__(f"confirm_count mismatch: expected {expected}, got {given}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return f"mem_{uuid4().hex[:24]}"


def _cat_id() -> str:
    return f"memcat_{uuid4().hex[:24]}"


@dataclass(frozen=True, slots=True)
class MemoryRecallHit:
    """One recall result — the memory row plus its similarity score.

    Kept as a frozen value object so callers don't mutate the memory
    row in place; writing back (e.g. ``touch_accessed``) goes through
    repo methods that respect session state."""

    memory: AssistantMemory
    score: float


@dataclass(frozen=True, slots=True)
class BroadcastHit:
    """Unified hit shape for ``recall_broadcast`` across multiple stores.

    ``source`` distinguishes the origin: ``memory:core`` /
    ``memory:episodic`` / ``checklist:<id>`` / ``reminder:<id>``.
    ``content`` is the canonical text the LLM sees (for checklists это
    ``"{list_title}: {item_title}"``; для reminder — title напоминания).
    ``embedding`` хранится для cross-source pairwise dedup и не
    используется callerom после возврата из ``recall_broadcast``.
    ``metadata`` carries source-specific extras (list_title для
    checklist, due_at для reminder, source_kind для memory).
    """

    source: str
    content: str
    score: float
    id: str
    embedding: list[float] = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceStats:
    """Per-source diagnostic counts for one ``recall_broadcast`` call."""

    count: int
    score_min: float
    score_max: float


def _empty_source_stats() -> SourceStats:
    return SourceStats(count=0, score_min=0.0, score_max=0.0)


def _source_stats(hits: list[BroadcastHit]) -> SourceStats:
    if not hits:
        return _empty_source_stats()
    scores = [h.score for h in hits]
    return SourceStats(
        count=len(hits),
        score_min=min(scores),
        score_max=max(scores),
    )


def _dedup_pairwise(
    hits: list[BroadcastHit], *, threshold: float = 0.95
) -> list[BroadcastHit]:
    """Drop hits whose embedding is within ``threshold`` cosine of an
    already-kept higher-scored hit.

    Иначе один и тот же факт ("брат др 18 мая"), сохранённый в core
    memory + продублированный как reminder, выскочит дважды и
    собьёт LLM.
    """
    if not hits:
        return []
    sorted_hits = sorted(hits, key=lambda h: h.score, reverse=True)
    keep: list[BroadcastHit] = []
    for h in sorted_hits:
        if any(
            cosine_similarity(h.embedding, k.embedding) > threshold
            for k in keep
        ):
            continue
        keep.append(h)
    return keep


@dataclass(frozen=True, slots=True)
class RecallStats:
    """Diagnostic stats for one recall() invocation.

    Used by ``node_load_memories`` (Stage 2 observability of the
    memory retrieval staged plan, see ``docs/tomorrow-plan.md`` пункт
    11). Lets us answer questions like:

    * how many memories does this user actually have on disk?
    * how many had embeddings (vs. needing re-embed)?
    * how many were dropped by ``min_score``?
    * what does the score distribution look like?

    All numeric. Logged structured so we can grep/awk later.
    """

    candidates_total: int       # all memories for (tenant, user, tier)
    with_embedding: int         # subset that produced a valid cosine score
                                # (= passed the JSON parse + has embedding_json
                                # gate; equivalent to filtered_below_min +
                                # len(passing_scores))
    filtered_below_min: int     # scored but dropped by min_score
    seeded_count: int           # final returned (after top_k cut)
    min_score: float            # threshold used
    top_k: int                  # cap used
    scores_min: float | None    # min over passing scores (None if none passed)
    scores_max: float | None    # max over passing scores
    scores_p50: float | None    # median over passing scores


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------------------------------------------------------------- categories

    def _find_common(self, tenant_id: str, user_id: str) -> MemoryCategory | None:
        # is_system И slug='common' (high m2): порченая system-строка не станет «навсегда Common».
        return (
            self.session.query(MemoryCategory)
            .filter_by(tenant_id=tenant_id, user_id=user_id, is_system=True, slug=COMMON_SLUG)
            .first()
        )

    def ensure_common(self, tenant_id: str, user_id: str) -> str:
        """#262: вернуть id системной Common-категории юзера; создать при отсутствии. Идемпотентно.

        Гонко-безопасно (4-5 параллельных fetch мини-аппа / параллельные ходы): SELECT → INSERT под
        savepoint; на гонке partial-unique `(tenant,user) WHERE is_system` бросит IntegrityError → откат
        savepoint (внешняя транзакция цела) → re-SELECT берёт победителя. Дёргается из save() и страницы."""
        row = self._find_common(tenant_id, user_id)
        if row is not None:
            return row.id
        cat = MemoryCategory(
            id=_cat_id(), tenant_id=tenant_id, user_id=user_id, slug=COMMON_SLUG,
            name=COMMON_DISPLAY_NAME,
            # C2: норма по имени (а не slug!) — иначе юзер создаст вторую «Общее» (norm «общий») без конфликта.
            name_normalized=COMMON_NAME_NORMALIZED,
            is_system=True, created_at=_utcnow(),
        )
        try:
            with self.session.begin_nested():  # savepoint — гонку не выносим во внешнюю транзакцию
                self.session.add(cat)
                self.session.flush()
            return cat.id
        except IntegrityError:  # гонка: другой создал Common первым → берём победителя
            winner = self._find_common(tenant_id, user_id)
            if winner is not None:
                return winner.id
            logger.warning(
                "ensure_common: IntegrityError but no Common for %s/%s (конфликт не по partial-unique system?)",
                tenant_id, user_id,
            )
            raise

    # NB (#262 A2): bulk-backfill существующих фактов в Common делает МИГРАЦИЯ 20260630_0071 инлайн-SQL (единственный
    # источник, исторически стабильный) — не дублируем его ORM-методом в репо (риск расхождения, ревью R1 M2).
    # Backfill валидируется сухим прогоном 0071 на Postgres-снапшоте (срез D). Горячий путь новых фактов —
    # ensure_common() выше, он же резолвит Common в save().

    # --------------------------------------------------------- categories CRUD

    def _own_category(self, tenant_id: str, user_id: str, category_id: str) -> MemoryCategory | None:
        """Категория, ТОЛЬКО если она принадлежит этому (tenant,user). Иначе None (→ API 404)."""
        return (
            self.session.query(MemoryCategory)
            .filter_by(id=category_id, tenant_id=tenant_id, user_id=user_id)
            .first()
        )

    def list_categories(self, tenant_id: str, user_id: str) -> list[MemoryCategory]:
        """Категории юзера: Common первой, дальше пользовательские по дате создания."""
        return (
            self.session.query(MemoryCategory)
            .filter_by(tenant_id=tenant_id, user_id=user_id)
            .order_by(MemoryCategory.is_system.desc(), MemoryCategory.created_at.asc())
            .all()
        )

    def get_category(self, tenant_id: str, user_id: str, category_id: str) -> MemoryCategory | None:
        """Категория этого (tenant,user) по id, иначе None. Публичный фасад над _own_category для API-слоя."""
        return self._own_category(tenant_id, user_id, category_id)

    def list_facts_in_category(
        self, tenant_id: str, user_id: str, category_id: str
    ) -> list[AssistantMemory]:
        """Факты одной категории (скоуп tenant+user+category), новые сверху. Ownership категории проверяет
        вызывающий через get_category (пустой список тут не отличает «чужая» от «своя пустая»)."""
        return (
            self.session.query(AssistantMemory)
            .filter_by(tenant_id=tenant_id, user_id=user_id, category_id=category_id)
            .order_by(AssistantMemory.created_at.desc())
            .all()
        )

    def get_memory(self, tenant_id: str, user_id: str, memory_id: str) -> AssistantMemory | None:
        """Факт этого (tenant,user) по id, иначе None. Публичный фасад над _own_memory — для API-слоя,
        чтобы проверить существование/владение ДО дорогих сайд-эффектов (синхронный re-embed при правке)."""
        return self._own_memory(tenant_id, user_id, memory_id)

    def create_category(self, tenant_id: str, user_id: str, name: str) -> MemoryCategory:
        """Создать пользовательскую категорию. Дубль имени (name_normalized) → CategoryNameConflict (409)."""
        display = (name or "").strip()
        norm = normalize_for_dedup(display)
        if not norm:
            raise ValueError("category name cannot be empty")
        if norm == COMMON_NAME_NORMALIZED:  # «Общее» зарезервировано за системной Common (R2 дыра порядка)
            raise CategoryNameConflict(display)
        cat = MemoryCategory(
            id=_cat_id(), tenant_id=tenant_id, user_id=user_id, slug=None,
            name=display, name_normalized=norm, is_system=False, created_at=_utcnow(),
        )
        try:
            with self.session.begin_nested():  # savepoint — конфликт не рвёт внешнюю транзакцию
                self.session.add(cat)
                self.session.flush()
        except IntegrityError as exc:
            raise CategoryNameConflict(display) from exc
        return cat

    def rename_category(
        self, tenant_id: str, user_id: str, category_id: str, name: str
    ) -> MemoryCategory | None:
        """Переименовать. None → 404; Common → CategoryImmutable; дубль → CategoryNameConflict."""
        cat = self._own_category(tenant_id, user_id, category_id)
        if cat is None:
            return None
        if cat.is_system:
            raise CategoryImmutable("Common cannot be renamed")
        display = (name or "").strip()
        norm = normalize_for_dedup(display)
        if not norm:
            raise ValueError("category name cannot be empty")
        if norm == COMMON_NAME_NORMALIZED:  # «Общее» зарезервировано за Common — нельзя переименовать в него
            raise CategoryNameConflict(display)
        if norm == cat.name_normalized:  # то же имя (с точностью до нормализации) — просто обновим display
            cat.name = display
            self.session.flush()
            return cat
        old_name, old_norm = cat.name, cat.name_normalized
        try:
            # C1: мутируем ВНУТРИ savepoint (симметрично create_category). Иначе при конфликте откат
            # savepoint оставляет внешнюю сессию dirty → PendingRollbackError на следующей операции → 500.
            with self.session.begin_nested():
                cat.name = display
                cat.name_normalized = norm
                self.session.flush()
        except IntegrityError as exc:
            cat.name, cat.name_normalized = old_name, old_norm  # вернуть in-memory к старому
            raise CategoryNameConflict(display) from exc
        return cat

    def count_facts_in_category(self, tenant_id: str, user_id: str, category_id: str) -> int:
        return (
            self.session.query(AssistantMemory)
            .filter_by(tenant_id=tenant_id, user_id=user_id, category_id=category_id)
            .count()
        )

    def delete_category(
        self, tenant_id: str, user_id: str, category_id: str, *, confirm_count: int
    ) -> int | None:
        """Удалить категорию ВМЕСТЕ с её фактами (решение владельца). Атомарно в одной транзакции.

        None → 404; Common → CategoryImmutable; confirm_count ≠ факт. числа → CategoryConfirmMismatch
        (набор изменился, защита от удаления вслепую). Возвращает число удалённых фактов (для server-confirm
        + аудита). Аудит-метаданные (id/счётчик, БЕЗ контента) пишет API-слой."""
        # M4 TOCTOU: сразу лочим строку категории FOR UPDATE (один fetch, R2) — конкурентный FK-insert факта
        # в неё (он берёт FOR KEY SHARE на родителя) блокируется до нашего коммита, после чего FK отвергнет
        # его (категории уже нет). Так is_system, count и delete читаются/делаются по ОДНОЙ залоченной строке
        # атомарно относительно confirm_count. SQLite: FOR UPDATE — no-op (тестам ок).
        cat = (
            self.session.query(MemoryCategory)
            .filter_by(id=category_id, tenant_id=tenant_id, user_id=user_id)
            .with_for_update()
            .one_or_none()
        )
        if cat is None:  # нет такой / чужая / удалена в гонке → 404
            return None
        if cat.is_system:
            raise CategoryImmutable("Common cannot be deleted")
        # R3: confirm_count сверяем с ФАКТИЧЕСКИМ rowcount удаления, а не с предварительным count. FOR UPDATE на
        # родителе не блокирует конкурентный move-out/delete дочернего факта между count и delete — поэтому
        # удаляем под savepoint и, если удалили НЕ ровно confirm_count, откатываем (факты целы) и бросаем
        # mismatch. Так «удалить ровно подтверждённое или ничего» — атомарно, без устаревшего счётчика.
        with self.session.begin_nested():
            deleted = (
                self.session.query(AssistantMemory)
                .filter_by(tenant_id=tenant_id, user_id=user_id, category_id=category_id)
                .delete(synchronize_session=False)
            )
            if deleted != confirm_count:
                raise CategoryConfirmMismatch(deleted, confirm_count)  # откатит savepoint (факты не тронуты)
            self.session.delete(cat)
            self.session.flush()
        return deleted

    # ------------------------------------------------------------- facts CRUD

    def _own_memory(self, tenant_id: str, user_id: str, memory_id: str) -> AssistantMemory | None:
        return (
            self.session.query(AssistantMemory)
            .filter_by(id=memory_id, tenant_id=tenant_id, user_id=user_id)
            .first()
        )

    def move_memory(
        self, tenant_id: str, user_id: str, memory_id: str, target_category_id: str
    ) -> AssistantMemory | None:
        """Перенести факт в другую СВОЮ категорию. None → 404 (нет факта ИЛИ чужая/несущ. цель)."""
        mem = self._own_memory(tenant_id, user_id, memory_id)
        if mem is None:
            return None
        if self._own_category(tenant_id, user_id, target_category_id) is None:
            return None  # цель чужая/не существует → composite FK всё равно бы отверг; явная 404
        mem.category_id = target_category_id
        self.session.flush()
        return mem

    def edit_memory(
        self,
        tenant_id: str,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        embedding: list[float] | None = None,
        clear_embedding_for_manual_reembed: bool = False,
    ) -> AssistantMemory | None:
        """Ручная правка факта: новый текст + ПЕРЕ-эмбеддинг + source=user_direct. None → 404.

        КОНТРАКТ (ревью R1 M5 / R2): вызывающий слой (API среза B) ОБЯЗАН вычислить новый embedding и передать
        его — как `save_core_fact` считает вектор перед `repo.save`. Сброс вектора в NULL делает факт
        невидимым для косинус-recall (durable reembed-джоба НЕТ — только ручной
        `scripts/reembed_all_with_current_model.py`), поэтому это НЕ молчаливый дефолт: чтобы сбросить вектор,
        надо ЯВНО передать `clear_embedding_for_manual_reembed=True`. Иначе пустой/отсутствующий вектор без
        флага → ValueError (защита от случайной потери recall, в т.ч. на `embedding=[]` от багнутого
        эмбеддера). Приёмка B: правленый факт остаётся находим recall'ом."""
        # `not embedding` ловит И None, И пустой список [] (R3: багнутый эмбеддер может вернуть [] вместо None,
        # а строка хранения ниже трактует [] как falsy → занулила бы вектор молча).
        if not embedding and not clear_embedding_for_manual_reembed:
            raise ValueError(
                "edit_memory requires a non-empty recomputed embedding (или явный "
                "clear_embedding_for_manual_reembed=True для деградированного режима)"
            )
        mem = self._own_memory(tenant_id, user_id, memory_id)
        if mem is None:
            return None
        text = (content or "").strip()
        if not text:
            raise ValueError("memory content cannot be empty")
        mem.content = text
        mem.embedding_json = json.dumps(embedding) if embedding else None
        mem.embedding_dim = len(embedding) if embedding else 0
        mem.source = "user_direct"  # пользователь правил вручную
        self.session.flush()
        return mem

    def delete_memory(self, tenant_id: str, user_id: str, memory_id: str) -> bool:
        """Удалить один факт (в т.ч. из Common). False → 404."""
        deleted = (
            self.session.query(AssistantMemory)
            .filter_by(id=memory_id, tenant_id=tenant_id, user_id=user_id)
            .delete(synchronize_session=False)
        )
        self.session.flush()
        return deleted > 0

    # ------------------------------------------------------------------- save

    def save(
        self,
        tenant_id: str,
        user_id: str,
        *,
        tier: str,
        content: str,
        embedding: list[float] | None = None,
        source: str = "agent_inferred",
        category_id: str | None = None,
    ) -> AssistantMemory:
        if tier not in MEMORY_TIERS:
            raise ValueError(f"unknown tier: {tier!r}")
        if source not in MEMORY_SOURCES:
            raise ValueError(f"unknown source: {source!r}")
        text = (content or "").strip()
        if not text:
            raise ValueError("memory content cannot be empty")
        # #262: факт без явной категории → Common (горячий путь tools.py, та же транзакция). С явной
        # категорией — префлайт владения (high m1): чужой/несущ. id → домен-404, а не IntegrityError/500.
        if category_id is None:
            category_id = self.ensure_common(tenant_id, user_id)
        elif self._own_category(tenant_id, user_id, category_id) is None:
            raise CategoryNotFound(category_id)

        row = AssistantMemory(
            id=_id(),
            tenant_id=tenant_id,
            user_id=user_id,
            tier=tier,
            content=text,
            embedding_json=json.dumps(embedding) if embedding else None,
            embedding_dim=len(embedding) if embedding else 0,
            source=source,
            category_id=category_id,
            created_at=_utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    # ------------------------------------------------------------------- read

    def list_by_user(
        self,
        tenant_id: str,
        user_id: str,
        *,
        tier: str | None = None,
        limit: int | None = None,
    ) -> list[AssistantMemory]:
        q = self.session.query(AssistantMemory).filter_by(
            tenant_id=tenant_id, user_id=user_id
        )
        if tier is not None:
            q = q.filter(AssistantMemory.tier == tier)
        q = q.order_by(AssistantMemory.created_at.desc())
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def get(self, memory_id: str) -> AssistantMemory | None:
        """INTERNAL, по id БЕЗ скоупа (M7). User-facing пути ОБЯЗАНЫ использовать `_own_memory`/`list_by_user`
        (скоуп по tenant+user) — иначе утечённый id заденет чужого тенанта."""
        return self.session.get(AssistantMemory, memory_id)

    # ----------------------------------------------------------------- recall

    def recall(
        self,
        tenant_id: str,
        user_id: str,
        query_embedding: list[float],
        *,
        tier: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[MemoryRecallHit]:
        """Cosine-similarity recall over the user's memories.

        Rows without a stored embedding are skipped (they'll need an
        out-of-band re-embed migration when embeddings get turned on).
        ``min_score`` filters out near-zero matches so the LLM context
        isn't polluted with irrelevant rows.

        Backward-compatible thin wrapper over ``recall_with_stats``;
        callers that don't need observability stats get the bare hit
        list as before. Use ``recall_with_stats`` when logging the
        retrieval-distribution diagnostics (e.g. ``node_load_memories``
        Stage 2).
        """
        hits, _stats = self.recall_with_stats(
            tenant_id, user_id, query_embedding,
            tier=tier, top_k=top_k, min_score=min_score,
        )
        return hits

    def recall_with_stats(
        self,
        tenant_id: str,
        user_id: str,
        query_embedding: list[float],
        *,
        tier: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> tuple[list[MemoryRecallHit], RecallStats]:
        """Same as ``recall`` but also returns ``RecallStats``.

        Stats are computed over the *full* candidate pool — useful for
        observability (Stage 2 of memory-retrieval roadmap). Score
        percentiles are over the rows that passed ``min_score`` (i.e.
        what the LLM might actually see), not the raw cosine
        distribution, because pre-filter scores include noise.
        """
        candidates = self.list_by_user(tenant_id, user_id, tier=tier)
        candidates_total = len(candidates)
        with_embedding = 0
        passing_scores: list[float] = []
        filtered_below_min = 0
        hits: list[MemoryRecallHit] = []
        for row in candidates:
            if not row.embedding_json:
                continue
            try:
                vec = json.loads(row.embedding_json)
            except json.JSONDecodeError:
                continue
            with_embedding += 1
            score = cosine_similarity(query_embedding, vec)
            if score < min_score:
                filtered_below_min += 1
                continue
            passing_scores.append(score)
            hits.append(MemoryRecallHit(memory=row, score=score))
        hits.sort(key=lambda h: h.score, reverse=True)
        seeded = hits[:top_k]
        scores_min = min(passing_scores) if passing_scores else None
        scores_max = max(passing_scores) if passing_scores else None
        scores_p50 = (
            sorted(passing_scores)[len(passing_scores) // 2]
            if passing_scores
            else None
        )
        stats = RecallStats(
            candidates_total=candidates_total,
            with_embedding=with_embedding,
            filtered_below_min=filtered_below_min,
            seeded_count=len(seeded),
            min_score=min_score,
            top_k=top_k,
            scores_min=scores_min,
            scores_max=scores_max,
            scores_p50=scores_p50,
        )
        return seeded, stats

    # ------------------------------------------------------ recall-broadcast

    def recall_broadcast(
        self,
        tenant_id: str,
        user_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 10,
        min_score: float = 0.1,
        include_structured: bool = True,
    ) -> tuple[list["BroadcastHit"], dict[str, "SourceStats"]]:
        """Fan-out recall across memory + checklist + reminder sources.

        Each source is queried independently inside try/except — one
        broken source never silently kills the whole recall. Pairwise
        dedup at cosine>0.95 drops near-duplicates (e.g. one fact saved
        to both core memory and a checklist). ``min_per_source=1``
        guarantee: any source with at least one hit above ``min_score``
        contributes at least one hit to the final list, regardless of
        global rank — so checklist/reminder hits don't get drowned out
        by uniformly-higher memory scores.

        Returns ``(hits, per_source_stats)``.
        """
        all_hits: list[BroadcastHit] = []
        per_source_stats: dict[str, SourceStats] = {}

        # source 1: assistant_memories
        try:
            mem_hits = self._recall_memory_source(
                tenant_id, user_id, query_embedding,
                top_k=7, min_score=min_score,
            )
            all_hits.extend(mem_hits)
            per_source_stats["memory"] = _source_stats(mem_hits)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recall_broadcast: memory source failed: %s", exc)
            per_source_stats["memory"] = _empty_source_stats()

        if include_structured:
            try:
                cl_hits = self._recall_checklist_source(
                    tenant_id, user_id, query_embedding,
                    top_k=3, min_score=min_score,
                )
                all_hits.extend(cl_hits)
                per_source_stats["checklist"] = _source_stats(cl_hits)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "recall_broadcast: checklist source failed: %s", exc
                )
                per_source_stats["checklist"] = _empty_source_stats()

            try:
                rem_hits = self._recall_reminder_source(
                    tenant_id, user_id, query_embedding,
                    top_k=3, min_score=min_score,
                )
                all_hits.extend(rem_hits)
                per_source_stats["reminder"] = _source_stats(rem_hits)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "recall_broadcast: reminder source failed: %s", exc
                )
                per_source_stats["reminder"] = _empty_source_stats()

        # cross-source pairwise dedup
        all_hits = _dedup_pairwise(all_hits, threshold=0.95)
        all_hits.sort(key=lambda h: h.score, reverse=True)

        # min_per_source=1 guarantee: представить каждый source ≥1 хитом
        final = list(all_hits[:top_k])
        represented = {h.source.split(":")[0] for h in final}
        by_source: dict[str, list[BroadcastHit]] = {}
        for h in all_hits:
            by_source.setdefault(h.source.split(":")[0], []).append(h)
        for source_prefix, hits in by_source.items():
            if source_prefix in represented:
                continue
            if not hits:
                continue
            top = hits[0]
            if top.score >= min_score:
                final.append(top)
                represented.add(source_prefix)
                logger.debug(
                    "recall_broadcast: min_per_source promoted %s score=%.2f",
                    source_prefix, top.score,
                )
        final.sort(key=lambda h: h.score, reverse=True)

        # observability — single log line in awk-friendly shape
        counts = {k: v.count for k, v in per_source_stats.items()}
        score_ranges = {
            k: f"{v.score_min:.2f}-{v.score_max:.2f}"
            for k, v in per_source_stats.items() if v.count
        }
        logger.info(
            "recall_broadcast tenant=%s user=%s "
            "source_counts=%s score_ranges=%s final_returned=%d",
            tenant_id, user_id, counts, score_ranges, len(final),
        )

        return final, per_source_stats

    def _recall_memory_source(
        self,
        tenant_id: str,
        user_id: str,
        query_vec: list[float],
        *,
        top_k: int,
        min_score: float,
    ) -> list["BroadcastHit"]:
        rows = self.list_by_user(tenant_id, user_id)
        hits: list[BroadcastHit] = []
        for row in rows:
            if not row.embedding_json:
                continue
            try:
                vec = json.loads(row.embedding_json)
            except json.JSONDecodeError:
                continue
            score = cosine_similarity(query_vec, vec)
            if score < min_score:
                continue
            hits.append(
                BroadcastHit(
                    source=f"memory:{row.tier}",
                    content=row.content,
                    score=score,
                    id=row.id,
                    embedding=vec,
                    metadata={"source_kind": row.source},
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def _recall_checklist_source(
        self,
        tenant_id: str,
        user_id: str,
        query_vec: list[float],
        *,
        top_k: int,
        min_score: float,
    ) -> list["BroadcastHit"]:
        # JOIN c checklists для tenant/user filter + title metadata.
        # Active checklists only: archived лежат вне scope обычного recall.
        # Items с любым status (pending/done/cancelled) — намеренно: юзер
        # может спросить «уже купила X?» и ответ «да, отметила как done»
        # требует, чтобы done items тоже были findable. metadata.item_status
        # позволяет LLM различить open/closed когда нужно.
        rows = (
            self.session.query(ChecklistItem, Checklist)
            .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
            .filter(
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
            )
            .all()
        )
        hits: list[BroadcastHit] = []
        for item, checklist in rows:
            if not item.embedding_json:
                continue
            try:
                vec = json.loads(item.embedding_json)
            except json.JSONDecodeError:
                continue
            score = cosine_similarity(query_vec, vec)
            if score < min_score:
                continue
            content = f"{checklist.title}: {item.title}"
            hits.append(
                BroadcastHit(
                    source=f"checklist:{checklist.id}",
                    content=content,
                    score=score,
                    id=item.id,
                    embedding=vec,
                    metadata={
                        "list_title": checklist.title,
                        "list_id": checklist.id,
                        "item_status": item.status,
                    },
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def _recall_reminder_source(
        self,
        tenant_id: str,
        user_id: str,
        query_vec: list[float],
        *,
        top_k: int,
        min_score: float,
    ) -> list["BroadcastHit"]:
        # Pending reminders only — fired/cancelled не имеют отношения к
        # «помнишь, я тебе говорила X» запросам пользователя.
        rows = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.tenant_id == tenant_id,
                FamilyReminder.user_id == user_id,
                FamilyReminder.status == "pending",
            )
            .all()
        )
        hits: list[BroadcastHit] = []
        for r in rows:
            if not r.embedding_json:
                continue
            try:
                vec = json.loads(r.embedding_json)
            except json.JSONDecodeError:
                continue
            score = cosine_similarity(query_vec, vec)
            if score < min_score:
                continue
            hits.append(
                BroadcastHit(
                    source=f"reminder:{r.id}",
                    content=r.title,
                    score=score,
                    id=r.id,
                    embedding=vec,
                    metadata={
                        "due_at": (
                            r.next_trigger_at.isoformat()
                            if r.next_trigger_at else None
                        ),
                        "recurrence_rule": r.recurrence_rule,
                    },
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    # ----------------------------------------------------------------- update

    def touch_accessed(self, memory_id: str) -> None:
        """INTERNAL, по id БЕЗ скоупа (M7). Дёргается только из recall-bookkeeping (graph.py) с id уже
        отскоупленного recall-хита; не для user-facing путей."""
        row = self.session.get(AssistantMemory, memory_id)
        if row is None:
            return
        row.access_count = (row.access_count or 0) + 1
        row.last_accessed_at = _utcnow()
        self.session.flush()

    def delete(self, memory_id: str) -> bool:
        """INTERNAL, по id БЕЗ скоупа (M7). User-facing удаление — ТОЛЬКО `delete_memory(tenant,user,id)`;
        этот footgun снёс бы чужой факт по утечённому id."""
        row = self.session.get(AssistantMemory, memory_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True
