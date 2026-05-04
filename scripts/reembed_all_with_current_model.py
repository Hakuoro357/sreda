#!/usr/bin/env python3
"""Re-embed ALL stored vectors using currently configured embedding model.

Запускается при смене embedding-провайдера/модели. Без этого старые
эмбеддинги остаются в чужой vector space и cosine с новыми query
векторами становится ~0 → recall_memory возвращает пусто (тот же баг
что мы поймали 2026-05-04 утром).

Сейчас (2026-05-04 PM) — переход с e5-large (neuraldeep.ru) на bge-m3
(cloud.ru). После этого скрипта BD homogeneously bge-m3.

Идемпотентно: можно запускать повторно. Каждая итерация overwrites
embedding_json + embedding_model полем для всех rows (не только NULL).

Throttle: 0.05s = 20 RPS = 10% от cloud.ru лимита 200 RPS. Достаточно
консервативно, чтобы не давить параллельный live traffic, но 261 row
завершается за ~15 сек вместо 9 минут.

Usage on prod:

    sudo systemd-run --uid=sreda --gid=sreda \\
        --working-directory=/opt/sreda \\
        -p EnvironmentFile=/etc/sreda/.env \\
        --wait --collect --pipe \\
        /opt/sreda/.venv/bin/python /opt/sreda/scripts/reembed_all_with_current_model.py
"""

from __future__ import annotations

import json
import logging
import sys
import time

from sqlalchemy import select

from sreda.db.models.checklists import Checklist, ChecklistItem
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.memory import AssistantMemory
from sreda.db.session import get_session_factory
from sreda.services.embeddings import (
    EMBEDDING_MODEL_NAME,
    DisabledEmbeddingClient,
    FakeEmbeddingClient,
    get_embeddings_client,
)


THROTTLE_SECONDS = 0.05  # 20 RPS, 10% of cloud.ru 200 RPS limit


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("reembed")
    log.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(h)
    return log


def main() -> int:
    log = _setup_logging()
    ec = get_embeddings_client()
    if isinstance(ec, (DisabledEmbeddingClient, FakeEmbeddingClient)):
        log.error(
            "Real embeddings client required (got %s).", type(ec).__name__
        )
        return 2

    log.info("client=%s target_model=%s", type(ec).__name__, EMBEDDING_MODEL_NAME)

    sf = get_session_factory()
    counts = {"memory": 0, "checklist_item": 0, "reminder": 0}
    failed = {"memory": 0, "checklist_item": 0, "reminder": 0}

    # 1. AssistantMemory
    with sf() as s:
        rows = s.execute(select(AssistantMemory)).scalars().all()
        log.info("assistant_memories total: %d", len(rows))
        for row in rows:
            try:
                vec = ec.embed_document(row.content)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed failed for memory=%s: %s", row.id, exc)
                failed["memory"] += 1
                continue
            row.embedding_json = json.dumps(vec)
            row.embedding_dim = len(vec)
            counts["memory"] += 1
            if counts["memory"] % 10 == 0:
                s.commit()
                log.info("memory: %d/%d done", counts["memory"], len(rows))
            time.sleep(THROTTLE_SECONDS)
        s.commit()

    # 2. ChecklistItem (concat with parent title)
    with sf() as s:
        rows = s.execute(
            select(ChecklistItem, Checklist)
            .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
        ).all()
        log.info("checklist_items total: %d", len(rows))
        for item, checklist in rows:
            text = f"{checklist.title}: {item.title}"
            try:
                vec = ec.embed_document(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed failed for item=%s: %s", item.id, exc)
                failed["checklist_item"] += 1
                continue
            item.embedding_json = json.dumps(vec)
            item.embedding_model = EMBEDDING_MODEL_NAME
            counts["checklist_item"] += 1
            if counts["checklist_item"] % 10 == 0:
                s.commit()
                log.info("checklist_item: %d/%d done", counts["checklist_item"], len(rows))
            time.sleep(THROTTLE_SECONDS)
        s.commit()

    # 3. FamilyReminder (structural prefix)
    with sf() as s:
        rows = s.execute(select(FamilyReminder)).scalars().all()
        log.info("family_reminders total: %d", len(rows))
        for rem in rows:
            text = f"Напоминание: {rem.title}"
            try:
                vec = ec.embed_document(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed failed for reminder=%s: %s", rem.id, exc)
                failed["reminder"] += 1
                continue
            rem.embedding_json = json.dumps(vec)
            rem.embedding_model = EMBEDDING_MODEL_NAME
            counts["reminder"] += 1
            if counts["reminder"] % 10 == 0:
                s.commit()
                log.info("reminder: %d/%d done", counts["reminder"], len(rows))
            time.sleep(THROTTLE_SECONDS)
        s.commit()

    log.info(
        "DONE memory=%d checklist_item=%d reminder=%d  failed=%s  model=%s",
        counts["memory"], counts["checklist_item"], counts["reminder"],
        failed, EMBEDDING_MODEL_NAME,
    )
    return 0 if sum(failed.values()) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
