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
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.checklists import (
    CHECKLIST_ITEM_STATUSES,
    CHECKLIST_STATUSES,
    Checklist,
    ChecklistItem,
)

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

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Checklist (родительский список)
    # ------------------------------------------------------------------

    def create_list(
        self, *, tenant_id: str, user_id: str, title: str,
    ) -> Checklist:
        clean = (title or "").strip()
        if not clean:
            raise ValueError("title required")
        if len(clean) > 200:
            clean = clean[:200]

        now = _utcnow()
        checklist = Checklist(
            id=f"checklist_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            user_id=user_id,
            title=clean,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(checklist)
        self.session.commit()
        return checklist

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

        now = _utcnow()
        added: list[ChecklistItem] = []
        for offset, title in enumerate(fresh):
            item = ChecklistItem(
                id=f"clitem_{uuid4().hex[:24]}",
                checklist_id=list_id,
                position=next_pos + offset,
                title=title[:1000],  # safety cap, EncryptedString вмещает Text
                status="pending",
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
