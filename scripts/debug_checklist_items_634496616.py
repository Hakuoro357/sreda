"""Read-only debug: dump all checklist items for tg_634496616.

Без UPDATE/DELETE — только чтение для диагностики рассинхрона.
"""
from sreda.db.session import get_db_session
from sreda.db.models.checklists import ChecklistItem


SHORT = {
    "checklist_3195dbe3ca87461ba77807fe": "archived: Дела",
    "checklist_7302d7b66011408d8186335b": "active:   Глобальные дела на даче",
    "checklist_f08cbbb603ff4dc39c5c4050": "active:   Забрать с дачи",
}


def main() -> None:
    sess = next(get_db_session())
    items = (
        sess.query(ChecklistItem)
        .filter(ChecklistItem.checklist_id.in_(SHORT.keys()))
        .order_by(ChecklistItem.checklist_id, ChecklistItem.position)
        .all()
    )
    print(f"Total items across all 3 lists: {len(items)}")
    print()
    for it in items:
        label = SHORT[it.checklist_id]
        print(
            f"{label} | [{it.status}] pos={it.position} "
            f"created={it.created_at} title={it.title!r}"
        )


if __name__ == "__main__":
    main()
