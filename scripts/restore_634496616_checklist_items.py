"""One-shot recovery: восстанавливает 3 пропавших пункта в чек-листе
«Глобальные дела на даче» юзера tg_634496616.

Контекст (incident 2026-04-28):
- 26.04 20:42:33 юзер сказал «Чинить забор» — LLM выдал text «Добавила»
  но НЕ вызвал add_checklist_items (tools=[]).
- 26.04 20:42:42 юзер сказал «сделать забор» — то же самое.
- Юзер видел в text-ответах что 4 пункта добавлены, на самом деле
  в БД только «Покрасить дом».

Восстанавливаем: «Чинить забор», «Сделать забор», «Разобрать кучу
глины» — те что LLM показывал в финальном тексте 28.04 15:02:17.
«Покрасить дом» уже есть в БД, dedup-логика add_items пропустит.
«Забрать с дачи» юзер сам удалил — не восстанавливаем.

Использование:
    python scripts/restore_634496616_checklist_items.py
"""
from sreda.db.session import get_db_session
from sreda.services.checklists import ChecklistService


CHECKLIST_ID = "checklist_7302d7b66011408d8186335b"
ITEMS_TO_RESTORE = [
    "Чинить забор",
    "Сделать забор",
    "Разобрать кучу глины",
]


def main() -> None:
    sess = next(get_db_session())
    svc = ChecklistService(sess)
    print(f"Restoring {len(ITEMS_TO_RESTORE)} items to {CHECKLIST_ID}...")
    created, skipped = svc.add_items(
        list_id=CHECKLIST_ID,
        items=ITEMS_TO_RESTORE,
    )
    print(f"Created: {len(created)}")
    for it in created:
        print(f"  + {it.id} pos={it.position} title={it.title!r}")
    if skipped:
        print(f"Skipped (dedup, already exist): {len(skipped)}")
        for t in skipped:
            print(f"  - {t!r}")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
