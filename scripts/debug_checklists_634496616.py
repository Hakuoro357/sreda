"""One-shot debug: dump checklists for tg_634496616.

Используется для исследования рассинхрона в Mini App у этого юзера
(2026-04-28). Не committable — артефакт расследования.
"""
from sreda.db.session import get_db_session
from sreda.db.models.checklists import Checklist, ChecklistItem


def main() -> None:
    sess = next(get_db_session())
    lists = (
        sess.query(Checklist)
        .filter_by(tenant_id="tenant_tg_634496616")
        .order_by(Checklist.created_at)
        .all()
    )
    print(f"Found {len(lists)} checklists for tenant_tg_634496616")
    print()
    for cl in lists:
        print(f"id={cl.id}")
        print(f"  status: {cl.status}")
        print(f"  created_at: {cl.created_at}")
        print(f"  title: {cl.title!r}")
        items = (
            sess.query(ChecklistItem)
            .filter_by(checklist_id=cl.id)
            .order_by(ChecklistItem.position)
            .all()
        )
        for it in items:
            print(
                f"    [{it.status}] pos={it.position} "
                f"created={it.created_at} title={it.title!r}"
            )
        print()


if __name__ == "__main__":
    main()
