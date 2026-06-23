"""ChecklistService — CRUD для именованных списков дел.

Паттерн повторяет TaskService:
  * commit-per-method — каждая мутация в своей транзакции (если LLM
    в середине turn'а вызвала бракованный tool, добрые предыдущие
    мутации уже сохранены).
  * fuzzy-резолверы по title — LLM передаёт «План кроя» текстом, а
    не id; резолвер находит активный список с самым близким именем.
  * EncryptedString на title/notes пунктов — контент = PII.

Связь с другими сервисами: НЕТ (Checklist — самостоятельная сущность).
В отличие от Task, у пункта нет связанного reminder и нет даты.
Если юзер хочет напоминание о пункте чек-листа — это отдельный flow:
``schedule_reminder`` напрямую (можно потом завести соединение, v1.2).

Recall-broadcast (Phase 2, 2026-05-04): items получают embedding через
optional ``embedding_client`` в конструкторе. Embed input —
``f"{checklist.title}: {item.title}"`` (R1#7 — короткий item без
title даёт плохой semantic signal). Failure tolerated: warning + NULL.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete as sa_delete, select as sa_select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.models.checklists import (
    CHECKLIST_ITEM_STATUSES,
    CHECKLIST_STATUSES,
    Checklist,
    ChecklistItem,
)
from sreda.services.embeddings import EMBEDDING_MODEL_NAME, EmbeddingClient

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise(text: str) -> str:
    """Lowercase + strip. Используем для fuzzy-сравнений «План кроя» ↔ «план кроя»."""
    return (text or "").strip().lower()


def _normalise_title(text: str) -> str:
    """Lowercase + strip + collapse internal whitespace.

    Используется для item-level dedup: «Купить  молоко» и «купить молоко»
    считаются одним. Применяется в add_items (2026-04-28 fix против
    дублей при move task → checklist).
    """
    return " ".join((text or "").lower().split())


class ChecklistService:
    """CRUD + поиск + статусные переходы для чек-листов и их пунктов."""

    def __init__(
        self,
        session: Session,
        *,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.session = session
        # Optional. Когда задан — items получают embedding на add/update.
        # None в legacy-callers (тесты, scripts) — items без embedding,
        # recall их найдёт только при backfill потом.
        self._embedding_client = embedding_client

    def _embed_item(
        self, checklist_title: str, item_title: str
    ) -> tuple[str | None, str | None]:
        """Generate embedding for a checklist item.

        Embed input combines list title + item text (R1#7 from qwen review):
        bare item like «молоко» has poor semantic signal; «Покупки: молоко»
        gives E5 the context to differentiate.

        Returns ``(embedding_json, model_name)`` or ``(None, None)`` on
        any failure — failure is non-fatal (item still saved without
        embedding; backfill or next update can fill it).
        """
        if self._embedding_client is None:
            return None, None
        text = f"{checklist_title}: {item_title}"
        try:
            vec = self._embedding_client.embed_document(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "checklist_item.embed failed (non-fatal): %s", exc
            )
            return None, None
        return json.dumps(vec), EMBEDDING_MODEL_NAME

    # ------------------------------------------------------------------
    # Checklist (родительский список)
    # ------------------------------------------------------------------

    def create_list(
        self, *, tenant_id: str, user_id: str, title: str,
    ) -> Checklist:
        """Create checklist OR return existing active list with same title.

        2026-04-29 (incident user_tg_352612382): LLM в одном turn'е
        сделал 2 tool-call'а — `create_checklist("X")` + затем
        `add_checklist_items("X", [...])`. add_checklist_items должен
        был fuzzy-match найти только что созданный список, но fuzzy
        матчинг иногда промахивается (пунктуация, регистр, добавочные
        пробелы), и создавался ВТОРОЙ list с тем же title. Юзер видел
        дубликат в Mini App «Дела».

        Dedup-правило: если у юзера уже есть active checklist с
        case-insensitive нормализованным title — возвращаем его, не
        создаём новый. Это идемпотентность в стиле «pb-кнопок» для
        write-tool'а.
        """
        clean = (title or "").strip()
        if not clean:
            raise ValueError("title required")
        if len(clean) > 200:
            clean = clean[:200]

        # Dedup: ищем active list с таким же title (case-insensitive,
        # collapsed whitespace). Маленькая SQL-нагрузка — у одного
        # юзера активных списков обычно <20.
        norm_target = " ".join(clean.lower().split())
        existing = (
            self.session.query(Checklist)
            .filter(
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
            )
            .all()
        )
        for cl in existing:
            if " ".join((cl.title or "").lower().split()) == norm_target:
                return cl

        # #202 семья checklists: ctx-путь (ReAct) оснащает op_id+hash (эталон recipes) — key-policy для
        # гейта Фазы 5 + аудит. pre-check по op_id (повтор tool_call → existing) и по hash (кросс-ходовой
        # семантический ключ-лемма; ловит морфовариант мимо whitespace-дедупа выше) делают ключи
        # ФУНКЦИОНАЛЬНЫМИ. Легаси (ctx None) → ключи None, поведение байт-в-байт. fail-closed + tenant-guard.
        from sreda.runtime.planner.tool_runtime import current_tool_runtime

        op_id_value: str | None = None
        nhash_value: str | None = None
        ctx = current_tool_runtime()
        if ctx is not None:
            if not user_id:
                raise ValueError("create_list ctx path: user_id обязателен (fail-closed).")
            if ctx.tenant_id and ctx.tenant_id != tenant_id:
                raise ValueError(
                    "create_list ctx path: ctx.tenant_id="
                    f"{ctx.tenant_id!r} != tenant_id={tenant_id!r} — leak across tenant.")
            from sreda.services.operation_id import (
                compute_normalized_title_hash,
                compute_operation_id_create,
            )
            from sreda.services.text_normalization import normalize_for_dedup

            op_id_value = compute_operation_id_create(
                plan_id=ctx.execution_id, step_id=ctx.step_id, action="create",
                entity_type="checklist", logical_key=normalize_for_dedup(clean))
            nhash_value = compute_normalized_title_hash(
                clean, entity_type="checklist", tenant_id=tenant_id,
                user_id=user_id or "") or None
            existing_op = (
                self.session.query(Checklist)
                .filter_by(tenant_id=tenant_id, user_id=user_id, operation_id=op_id_value)
                .one_or_none()
            )
            if existing_op is not None:
                return existing_op
            if nhash_value is not None:
                existing_hash = (
                    self.session.query(Checklist)
                    .filter_by(tenant_id=tenant_id, user_id=user_id,
                               normalized_title_hash=nhash_value, status="active")
                    .first()
                )
                if existing_hash is not None:
                    return existing_hash

        now = _utcnow()
        checklist = Checklist(
            id=f"checklist_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            title=clean,
            status="active",
            created_at=now,
            updated_at=now,
            operation_id=op_id_value,  # #202: key-policy (ctx) / None (легаси)
            normalized_title_hash=nhash_value,
        )
        self.session.add(checklist)
        try:
            self.session.commit()
        except IntegrityError:
            # Гонка по operation_id — на сингл-тенанте ReAct не ожидается, fail-safe: откат + replay.
            self.session.rollback()
            if op_id_value is not None:
                racer = (
                    self.session.query(Checklist)
                    .filter_by(tenant_id=tenant_id, user_id=user_id, operation_id=op_id_value)
                    .one_or_none()
                )
                if racer is not None:
                    return racer
            raise
        return checklist

    def create_list_no_commit(
        self,
        *,
        tenant_id: str,
        user_id: str,
        title: str,
        force_new: bool = False,
    ) -> Checklist:
        """R-33 (2026-05-15): create_list variant без internal commit.

        Caller MUST own the transaction. Used by ``TaskService.add_task_with_details``
        для composite atomic create (task + checklist + items + link в одной TX).

        ``force_new=True`` (default False для backward-compat):
        - Bypass title-dedup. Если существует active list с same title — НЕ reuse,
          создаём fresh с уникальным suffix («Крой», «Крой (2)», «Крой (3)»).
        - Required для R-33 composite path: иначе ChecklistService может attach
          new task к old unlinked checklist'у с тем же title (например, «Крой»
          существует с 13.05; user 17.05 говорит «крой: ...» — мы хотим NEW
          checklist для 17.05, не reuse 13.05's items).

        ``force_new=False`` (legacy semantics):
        - Same dedup as ``create_list``: returns existing active list с same
          normalized title если найден.

        Returns Checklist (flushed but not committed). Caller commits.
        """
        clean = (title or "").strip()
        if not clean:
            raise ValueError("title required")
        if len(clean) > 200:
            clean = clean[:200]

        if not force_new:
            # Legacy dedup path
            norm_target = " ".join(clean.lower().split())
            existing_lists = (
                self.session.query(Checklist)
                .filter(
                    Checklist.tenant_id == tenant_id,
                    Checklist.user_id == user_id,
                    Checklist.status == "active",
                )
                .all()
            )
            for cl in existing_lists:
                if " ".join((cl.title or "").lower().split()) == norm_target:
                    return cl

        if force_new:
            # Find unique title с suffix on collision
            norm_target = " ".join(clean.lower().split())
            existing_titles = {
                " ".join((cl.title or "").lower().split())
                for cl in self.session.query(Checklist)
                .filter(
                    Checklist.tenant_id == tenant_id,
                    Checklist.user_id == user_id,
                    Checklist.status == "active",
                )
                .all()
            }
            final_title = clean
            if norm_target in existing_titles:
                # Append «(N)» suffix until unique
                for n in range(2, 1000):
                    candidate = f"{clean} ({n})"
                    if " ".join(candidate.lower().split()) not in existing_titles:
                        final_title = candidate
                        break
                else:
                    raise RuntimeError(
                        f"create_list_no_commit: cannot find unique title "
                        f"after 1000 suffix attempts (tenant={tenant_id} "
                        f"base={clean!r})"
                    )
        else:
            final_title = clean

        now = _utcnow()
        checklist = Checklist(
            id=f"checklist_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            title=final_title,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(checklist)
        self.session.flush()  # get id, NO commit — caller owns TX
        return checklist

    def add_items_no_commit(
        self, *, list_id: str, items: list[str],
    ) -> tuple[list[ChecklistItem], list[str]]:
        """R-33 (2026-05-15): add_items variant без internal commit.

        Caller MUST own the transaction. Same semantics as ``add_items``
        (skip empty, dedup against existing item titles в той же checklist,
        return (created, skipped_dup)) но flush'ит без commit.

        Returns (created_items, skipped_duplicate_titles).
        """
        cl = self.session.get(Checklist, list_id)
        if cl is None:
            raise ValueError(f"checklist {list_id!r} not found")

        cleaned: list[str] = []
        for raw in items or []:
            s = (raw or "").strip()
            if s:
                cleaned.append(s[:200] if len(s) > 200 else s)
        if not cleaned:
            return [], []

        # Existing items для dedup-проверки
        existing_items = (
            self.session.query(ChecklistItem)
            .filter(ChecklistItem.checklist_id == cl.id)
            .all()
        )
        existing_norm = {
            " ".join((it.title or "").lower().split())
            for it in existing_items
        }
        max_pos = max(
            (it.position for it in existing_items), default=-1
        )

        now = _utcnow()
        created: list[ChecklistItem] = []
        skipped_dup: list[str] = []
        position = max_pos + 1
        for title in cleaned:
            norm = " ".join(title.lower().split())
            if norm in existing_norm:
                skipped_dup.append(title)
                continue
            existing_norm.add(norm)
            item = ChecklistItem(
                id=f"clitem_{uuid4().hex[:24]}",
                checklist_id=cl.id,
                position=position,
                title=title,
                status="pending",
                created_at=now,
                updated_at=now,
            )
            self.session.add(item)
            created.append(item)
            position += 1

        cl.updated_at = now
        self.session.flush()  # no commit
        return created, skipped_dup

    def archive_list(
        self, *, tenant_id: str, user_id: str, list_id: str,
    ) -> Checklist | None:
        cl = self._get_owned(tenant_id, user_id, list_id)
        if cl is None:
            return None
        cl.status = "archived"
        cl.updated_at = _utcnow()
        self.session.commit()
        return cl

    def delete_list(
        self, *, tenant_id: str, user_id: str, list_id: str,
    ) -> bool:
        """Hard delete + явное удаление items (CASCADE требует PRAGMA
        foreign_keys=ON в SQLite — на всех бэкендах не гарантирован)."""
        cl = self._get_owned(tenant_id, user_id, list_id)
        if cl is None:
            return False
        self.session.query(ChecklistItem).filter(
            ChecklistItem.checklist_id == cl.id
        ).delete(synchronize_session=False)
        self.session.delete(cl)
        self.session.commit()
        return True

    def list_active(
        self, *, tenant_id: str, user_id: str,
    ) -> list[Checklist]:
        return (
            self.session.query(Checklist)
            .filter(
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
            )
            .order_by(Checklist.created_at.desc())
            .all()
        )

    def find_list_by_title(
        self, *, tenant_id: str, user_id: str, needle: str,
    ) -> Checklist | None:
        """Fuzzy match: substring (case-insensitive) от needle.

        Стратегия:
          1. Если needle совпал с id чек-листа (`checklist_xxx`) —
             возвращаем напрямую.
          2. Иначе ищем активные чек-листы и берём первый, чей
             title содержит needle (или наоборот). Берём самый
             свежий по created_at если матчей несколько.
        """
        if not needle:
            return None
        clean = needle.strip()
        if not clean:
            return None

        # 1) точный id?
        if clean.startswith("checklist_"):
            cl = self.session.get(Checklist, clean)
            if cl and cl.tenant_id == tenant_id and cl.user_id == user_id:
                return cl

        # 2) substring match
        norm = _normalise(clean)
        active = self.list_active(tenant_id=tenant_id, user_id=user_id)
        for cl in active:
            if norm in _normalise(cl.title) or _normalise(cl.title) in norm:
                return cl
        return None

    # ------------------------------------------------------------------
    # ChecklistItem (пункты внутри списка)
    # ------------------------------------------------------------------

    def add_items(
        self, *, list_id: str, items: list[str],
    ) -> tuple[list[ChecklistItem], list[str]]:
        """Пакетно добавляет пункты в конец списка.

        Авто-position: max(position)+1, +1 для каждого следующего.
        Пустые/whitespace-only items пропускаются.

        2026-04-28: добавлен dedup. Возвращает кортеж
        ``(created, skipped_titles)``:
        * ``created``        — реально вставленные ChecklistItem
        * ``skipped_titles`` — пункты которые НЕ добавлены т.к.
          такой title уже есть в списке (case-insensitive,
          whitespace-collapsed). В частности ловит сценарий
          tg_634496616 (move task → итог в чек-листе ДВАЖДЫ).

        Dedup проверяется против ВСЕХ статусов (pending/done/cancelled)
        — если пункт был и done, повторно его не нужно. Также внутри
        самого batch'а: если LLM передал «купить молоко» дважды — берём
        один.
        """
        clean = [(i or "").strip() for i in items]
        clean = [i for i in clean if i]
        if not clean:
            return [], []

        # Existing items для dedup'а — все статусы.
        existing_norms: set[str] = {
            _normalise_title(it.title)
            for it in (
                self.session.query(ChecklistItem.title)
                .filter(ChecklistItem.checklist_id == list_id)
                .all()
            )
        }

        seen_in_batch: set[str] = set()
        skipped_titles: list[str] = []
        fresh: list[str] = []
        for title in clean:
            norm = _normalise_title(title)
            if norm in existing_norms or norm in seen_in_batch:
                skipped_titles.append(title)
                continue
            seen_in_batch.add(norm)
            fresh.append(title)

        if not fresh:
            return [], skipped_titles

        # Берём текущий max position в списке.
        max_pos = (
            self.session.query(ChecklistItem.position)
            .filter(ChecklistItem.checklist_id == list_id)
            .order_by(ChecklistItem.position.desc())
            .first()
        )
        next_pos = (max_pos[0] + 1) if max_pos else 0

        # Title родительского списка для embedding input (Phase 2).
        # Если list_id не находится — пропускаем embedding, items без
        # него по-прежнему создадутся (graceful).
        checklist_title: str | None = None
        if self._embedding_client is not None:
            parent = self.session.get(Checklist, list_id)
            checklist_title = parent.title if parent else None

        now = _utcnow()
        added: list[ChecklistItem] = []
        for offset, title in enumerate(fresh):
            clean_title = title[:1000]  # safety cap, EncryptedString вмещает Text
            embedding_json: str | None = None
            embedding_model: str | None = None
            if checklist_title is not None:
                embedding_json, embedding_model = self._embed_item(
                    checklist_title, clean_title
                )
            item = ChecklistItem(
                id=f"clitem_{uuid4().hex[:24]}",
                checklist_id=list_id,
                position=next_pos + offset,
                title=clean_title,
                status="pending",
                embedding_json=embedding_json,
                embedding_model=embedding_model,
                created_at=now,
                updated_at=now,
            )
            self.session.add(item)
            added.append(item)

        self.session.commit()
        return added, skipped_titles

    def list_items(
        self,
        *,
        list_id: str,
        status: str | None = None,
    ) -> list[ChecklistItem]:
        q = self.session.query(ChecklistItem).filter(
            ChecklistItem.checklist_id == list_id,
        )
        if status is not None:
            q = q.filter(ChecklistItem.status == status)
        return q.order_by(ChecklistItem.position.asc()).all()

    def find_item_by_title(
        self,
        *,
        list_id: str,
        needle: str,
        only_pending: bool = True,
    ) -> ChecklistItem | None:
        """Fuzzy match пункта по подстроке. Если ``only_pending=True``
        ищем только среди не-выполненных (типичный кейс — «Закройила
        лаванду» → найти pending пункт со словом «лаванда»)."""
        if not needle:
            return None
        clean = needle.strip()
        if not clean:
            return None

        if clean.startswith("clitem_"):
            it = self.session.get(ChecklistItem, clean)
            if it and it.checklist_id == list_id:
                return it

        norm = _normalise(clean)
        items = self.list_items(
            list_id=list_id,
            status="pending" if only_pending else None,
        )
        for it in items:
            if norm in _normalise(it.title):
                return it
        # Fallback: попробовать без only_pending фильтра
        if only_pending:
            for it in self.list_items(list_id=list_id):
                if norm in _normalise(it.title):
                    return it
        return None

    def mark_done(self, *, item_id: str) -> ChecklistItem | None:
        item = self.session.get(ChecklistItem, item_id)
        if item is None:
            return None
        item.status = "done"
        item.done_at = _utcnow()
        item.updated_at = _utcnow()
        self.session.commit()
        return item

    def undo_done(self, *, item_id: str) -> ChecklistItem | None:
        item = self.session.get(ChecklistItem, item_id)
        if item is None:
            return None
        item.status = "pending"
        item.done_at = None
        item.updated_at = _utcnow()
        self.session.commit()
        return item

    def cancel_item(self, *, item_id: str) -> ChecklistItem | None:
        item = self.session.get(ChecklistItem, item_id)
        if item is None:
            return None
        item.status = "cancelled"
        item.updated_at = _utcnow()
        self.session.commit()
        return item

    def delete_item(self, *, item_id: str) -> bool:
        """Hard delete пункта (например, юзер сказал «удали пункт X» —
        запись неверная, не нужна вообще). Отличается от mark_done
        (status=done) и cancel_item (status=cancelled, виден с ✗).
        После delete пункт исчезает из всех list_items / show_checklist."""
        item = self.session.get(ChecklistItem, item_id)
        if item is None:
            return False
        self.session.delete(item)
        self.session.commit()
        return True

    # ------------------------------------------------------------------
    # Сводки (для home-card / Mini App)
    # ------------------------------------------------------------------

    def count_open_items(
        self, *, tenant_id: str, user_id: str,
    ) -> int:
        """Кол-во pending-пунктов во всех активных чек-листах юзера.
        Для home-card subtitle типа «N открытых пунктов»."""
        return (
            self.session.query(ChecklistItem)
            .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
            .filter(
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
                ChecklistItem.status == "pending",
            )
            .count()
        )

    def count_active_lists(
        self, *, tenant_id: str, user_id: str,
    ) -> int:
        return (
            self.session.query(Checklist)
            .filter(
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
            )
            .count()
        )

    def list_summary(
        self, *, list_id: str,
    ) -> tuple[int, int, int]:
        """Возвращает (pending, done, total) — для отображения «7 пунктов · 2 готово»."""
        items = self.list_items(list_id=list_id)
        pending = sum(1 for i in items if i.status == "pending")
        done = sum(1 for i in items if i.status == "done")
        return pending, done, len(items)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_owned(
        self, tenant_id: str, user_id: str, list_id: str,
    ) -> Checklist | None:
        """Cross-tenant-safe single-row fetch."""
        return (
            self.session.query(Checklist)
            .filter(
                Checklist.id == list_id,
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
            )
            .one_or_none()
        )

    def get_owned_item(
        self, *, item_id: str, tenant_id: str, user_id: str,
    ) -> ChecklistItem | None:
        """#143 Phase B: достать пункт, ПОДТВЕРДИВ владельца через
        родительский список И что список АКТИВЕН.

        У ``ChecklistItem`` нет собственных ``tenant_id``/``user_id`` —
        принадлежность определяется родительским ``Checklist``. JOIN'им и
        фильтруем по tenant И user, поэтому чужой пункт (другая семья /
        другой пользователь) НИКОГДА не вернётся. Это страж изоляции
        семей для id-пути mark/delete (launch-инвариант): планировщик
        получает item_id из ``list_checklist_items`` (где выдача УЖЕ
        ограничена контекстом), но мутирующий рантайм всё равно
        перепроверяет владельца ДО изменения — id мог прийти из старого
        плана / другого turn'а.

        Фильтр ``Checklist.status == "active"`` (Codex review #143 B):
        ``list_checklist_items`` показывает пункты ТОЛЬКО активных
        списков, значит mutate-инвариант — «менять можно лишь то, что мог
        выбрать читающий шаг». По stale id из АРХИВНОГО списка пункт не
        вернётся (и не будет изменён/удалён)."""
        return (
            self.session.query(ChecklistItem)
            .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
            .filter(
                ChecklistItem.id == item_id,
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
            )
            .one_or_none()
        )

    def _owned_active_item_exists(self, *, tenant_id: str, user_id: str):
        """Коррелированный EXISTS: пункт принадлежит tenant+user и его список
        АКТИВЕН. Встраивается прямо в WHERE мутации (одно выражение — нет
        окна гонки между проверкой и изменением)."""
        return (
            sa_select(Checklist.id)
            .where(
                Checklist.id == ChecklistItem.checklist_id,
                Checklist.tenant_id == tenant_id,
                Checklist.user_id == user_id,
                Checklist.status == "active",
            )
            .exists()
        )

    def mark_owned_item_done(
        self, *, item_id: str, tenant_id: str, user_id: str,
    ) -> tuple[str, str] | None:
        """#143 Phase B (Codex review R2, MAJOR): АТОМАРНАЯ отметка «сделано»
        ОДНИМ ``UPDATE ... WHERE id AND EXISTS(active owned checklist)
        RETURNING``.

        Прежняя версия делала ``get_owned_item`` (SELECT) → мутация ORM →
        commit: между SELECT и UPDATE список могли архивировать или пункт
        удалить (active-фильтр уже НЕ участвовал в самой мутации) → гонка
        могла изменить пункт архивного списка либо уйти в StaleDataError/
        internal вместо мягкого not_found (критерий #8). Теперь ownership +
        активность + мутация — в ОДНОМ выражении; пустой RETURNING → ``None``.

        Возвращает ``(item_id, title)`` обновлённого пункта или ``None``
        (чужой / архивный / исчезнувший — мутации НЕ происходит)."""
        now = _utcnow()
        stmt = (
            sa_update(ChecklistItem)
            .where(
                ChecklistItem.id == item_id,
                self._owned_active_item_exists(tenant_id=tenant_id, user_id=user_id),
            )
            .values(status="done", done_at=now, updated_at=now)
            .returning(ChecklistItem.id, ChecklistItem.title)
            .execution_options(synchronize_session=False)
        )
        row = self.session.execute(stmt).first()
        self.session.commit()
        return (row[0], row[1]) if row is not None else None

    def delete_owned_item(
        self, *, item_id: str, tenant_id: str, user_id: str,
    ) -> tuple[str, str] | None:
        """#143 Phase B (Codex review R2, MAJOR): АТОМАРНОЕ удаление ОДНИМ
        ``DELETE ... WHERE id AND EXISTS(active owned checklist) RETURNING``
        (см. ``mark_owned_item_done`` про окно гонки). Возвращает
        ``(item_id, title)`` удалённого пункта (чтобы собрать ответ без
        pre-fetch) или ``None`` (чужой / архивный / исчезнувший)."""
        stmt = (
            sa_delete(ChecklistItem)
            .where(
                ChecklistItem.id == item_id,
                self._owned_active_item_exists(tenant_id=tenant_id, user_id=user_id),
            )
            .returning(ChecklistItem.id, ChecklistItem.title)
            .execution_options(synchronize_session=False)
        )
        row = self.session.execute(stmt).first()
        self.session.commit()
        return (row[0], row[1]) if row is not None else None
