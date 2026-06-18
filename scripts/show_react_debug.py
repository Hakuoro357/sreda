#!/usr/bin/env python
"""vex#170: показать сохранённую переписку react-хода (вопрос + ответ бота + инструменты).

Читает react_debug_turns через ORM (текст авто-расшифровывается). Запуск на VDS:
  systemd-run -p EnvironmentFile=/etc/sreda/.env -p User=sreda -p WorkingDirectory=/opt/sreda \
    --setenv=PYTHONPATH=/opt/sreda/src /opt/sreda/.venv/bin/python scripts/show_react_debug.py \
    <tenant_id|user_id> [minutes]
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from sreda.db.models import ReactDebugTurn
from sreda.db.session import get_session_factory


def main() -> None:
    who = sys.argv[1] if len(sys.argv) > 1 else ""
    minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    s = get_session_factory()()
    cut = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    try:
        rows = (s.query(ReactDebugTurn)
                .filter(((ReactDebugTurn.tenant_id == who) | (ReactDebugTurn.user_id == who)),
                        ReactDebugTurn.created_at > cut)
                .order_by(ReactDebugTurn.created_at).all())
        print(f"react_debug_turns: {len(rows)} за {minutes} мин для {who!r}")
        for r in rows:
            ts = r.created_at.strftime("%H:%M:%S")
            print(f"\n[{ts}] {r.channel}/{r.kind} tools={r.tools_json}")
            print(f"  ЮЗЕР: {r.user_text!r}")
            print(f"  БОТ : {r.reply_text!r}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
