"""Probe MAX Bot API — research script for Phase 0 of MAX integration.

Цели:
1. Подтвердить auth header format (`Authorization: <token>`)
2. Изучить структуру Update / Message
3. Найти правильный формат `recipient` для POST /messages
4. Проверить поддержку `secret_token` в POST /subscriptions
5. Проверить наличие `phone` в Update
6. Test deep-link `start_param` delivery (новый и repeat user)
7. Test callback_data size limit

Скрипт безопасен для запуска на проде (read-only + одноразовый
test message в наш бот). Выводит markdown-friendly findings, которые
запишем в `docs/research/max_api_contracts.md`.

Запуск:
    cd /opt/sreda
    sudo systemd-run --uid=sreda --gid=sreda --working-directory=/opt/sreda \
      -p EnvironmentFile=/etc/sreda/.env --wait --collect --pipe -- \
      /opt/sreda/.venv/bin/python /opt/sreda/scripts/dev/probe_max_api.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx


BASE = "https://platform-api.max.ru"


def _hdr(token: str) -> dict[str, str]:
    return {
        "Authorization": token,  # БЕЗ Bearer per docs
        "Content-Type": "application/json",
    }


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def get(client: httpx.Client, path: str, **params: Any) -> dict | list:
    r = client.get(f"{BASE}{path}", params=params, timeout=20.0)
    print(f"  GET {path}?{params} → {r.status_code}")
    if r.status_code != 200:
        print(f"  body: {r.text[:500]}")
        return {}
    return r.json()


def post(client: httpx.Client, path: str, payload: dict) -> tuple[int, Any]:
    r = client.post(f"{BASE}{path}", json=payload)
    print(f"  POST {path} {payload!r} → {r.status_code}")
    if r.status_code >= 400:
        print(f"  body: {r.text[:500]}")
        return r.status_code, None
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def main() -> int:
    token = os.environ.get("SREDA_MAX_BOT_TOKEN")
    if not token:
        print("FATAL: SREDA_MAX_BOT_TOKEN не задан в env")
        return 1

    print(f"# MAX API Probe ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    print(f"token prefix: {token[:8]}...")

    with httpx.Client(headers=_hdr(token), timeout=15.0) as client:
        # ── 1. /me — confirm auth shape ──────────────────────────────
        section("1. GET /me — auth confirmation")
        me = get(client, "/me")
        if me:
            print(f"  bot info: {json.dumps(me, indent=2, ensure_ascii=False)}")

        # ── 2. /updates — structure of Update / Message ─────────────
        # timeout=0 → non-blocking; иначе long-poll держит соединение до
        # любого нового update (default 30s).
        section("2. GET /updates?limit=5&timeout=0 — Update / Message structure")
        updates = get(client, "/updates", limit=5, timeout=0)
        print(f"  raw: {json.dumps(updates, indent=2, ensure_ascii=False)[:2000]}")

        # ── 3. /subscriptions — текущие webhook subscriptions ──────
        section("3. GET /subscriptions — current webhooks")
        subs = get(client, "/subscriptions")
        print(f"  current: {json.dumps(subs, indent=2, ensure_ascii=False)}")

        # ── 4. POST /subscriptions с secret_token (test support) ───
        # NOT actually registering — testing how MAX reacts to secret_token field
        section("4. POST /subscriptions с secret_token field (test)")
        print("  → Skipped: don't want to actually create webhook in probe")
        print("  → Run separately with curl на staging URL когда дойдём до Phase 5")

        # ── 5. POST /messages — recipient format experiments ───────
        # Skip actual send — we'd spam our own bot. Document expectations:
        section("5. POST /messages recipient format")
        print("  → Документация говорит recipient — объект, не chat_id flat")
        print("  → Возможные формы: {chat_id: ...} | {user_id: ...} |")
        print("    {chat_id: ..., user_id: ...} (combo)")
        print("  → Уточнить опытным путём в Phase 2 при первом отправе")

        # ── 6. Search for 'phone' field in Update structure ──────
        section("6. Phone field in Update?")
        if isinstance(updates, dict) and "updates" in updates:
            for upd in updates["updates"]:
                phone_keys = [k for k in str(upd) if "phone" in str(upd).lower()]
                if "phone" in str(upd).lower():
                    print(f"  Found 'phone' mention в update: keys = {phone_keys}")
                    break
            else:
                print("  → No 'phone' в существующих updates (probably user_id only)")

        # ── 7. Document deep-link probe steps ─────────────────────
        section("7. Deep-link start_param probe (manual step)")
        print("  Manual probe needed:")
        print("  a) Открыть https://max.ru/id320700072280_bot?startapp=test_first")
        print("     с тест-аккаунта № 1 (ранее не писал боту)")
        print("  b) Запустить probe снова, в /updates ищем event с start_param=test_first")
        print("  c) Открыть тот же deep-link с другим payload (test_second)")
        print("     с тем же тест-аккаунтом (теперь existing)")
        print("  d) Запустить probe снова — проверить какой event приходит,")
        print("     fires ли bot_started повторно или приходит другой event")
        print("  Записать findings в docs/research/max_api_contracts.md")

        # ── 8. callback_data size limit (manual via send_message tests) ─
        section("8. callback_data size limit")
        print("  → Нужен отдельный send-тест после Phase 2 (когда есть MaxClient)")
        print("  → Tg limit: 64 bytes. MAX: undocumented, проверить эмпирически")
        print("  → Если меньше 64 — channel-linking использует short token-ID")

    print()
    print("=" * 60)
    print("Probe done. Заполнить docs/research/max_api_contracts.md")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
