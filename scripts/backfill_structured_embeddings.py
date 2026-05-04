#!/usr/bin/env python3
"""Backfill embeddings for existing checklist_items + family_reminders.

Recall-broadcast Phase 1+6 (2026-05-04 plan
``recall-broadcast-fanout-final.md``).

Pre-flight:
- Migration ``20260504_0037`` already applied (columns exist NULL).
- Phase 2 write-side hooks already deployed (new rows get embeddings;
  this script only fills legacy rows where ``embedding_json IS NULL``).
- ``SREDA_EMBEDDINGS_*`` configured (real client returned, not Disabled).

Throttle: 1 request per 2 seconds — keeps under neuraldeep 250/hour.
For ~600 rows = ~20 minutes. Idempotent: WHERE embedding_json IS NULL
guard ensures re-running is safe.

Usage on prod:

    sudo systemd-run --uid=sreda --gid=sreda \\
        --working-directory=/opt/sreda \\
        -p EnvironmentFile=/etc/sreda/.env \\
        --wait --collect --pipe \\
        /opt/sreda/.venv/bin/python /opt/sreda/scripts/backfill_structured_embeddings.py
"""

from __future__ import annotations

import json
import logging
import sys
import time

from sqlalchemy import select

from sreda.db.models.checklists import Checklist, ChecklistItem
from sreda.db.models.housewife import FamilyReminder
from sreda.db.session import get_session_factory
from sreda.services.embeddings import (
    EMBEDDING_MODEL_NAME,
    DisabledEmbeddingClient,
    FakeEmbeddingClient,
    get_embeddings_client,
)


THROTTLE_SECONDS = 0.05  # 20 RPS, 10% of cloud.ru 200 RPS limit
MODEL_NAME = EMBEDDING_MODEL_NAME


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("backfill")
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
            "Real embeddings client required (got %s). "
            "Set SREDA_EMBEDDINGS_BASE_URL + SREDA_EMBEDDINGS_MODEL "
            "before running backfill.",
            type(ec).__name__,
        )
        return 2

    log.info("client=%s", type(ec).__name__)

    sf = get_session_factory()
    embedded_items = 0
    embedded_reminders = 0

    # checklist_items: JOIN with checklists for title context
    with sf() as s:
        rows = s.execute(
            select(ChecklistItem, Checklist)
            .join(Checklist, ChecklistItem.checklist_id == Checklist.id)
            .where(ChecklistItem.embedding_json.is_(None))
        ).all()
        log.info("checklist_items NULL embeddings: %d", len(rows))
        for item, checklist in rows:
            text = f"{checklist.title}: {item.title}"
            try:
                vec = ec.embed_document(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed failed for item=%s: %s", item.id, exc)
                continue
            item.embedding_json = json.dumps(vec)
            item.embedding_model = MODEL_NAME
            embedded_items += 1
            if embedded_items % 10 == 0:
                s.commit()
                log.info("checklist_items: %d/%d embedded", embedded_items, len(rows))
            time.sleep(THROTTLE_SECONDS)
        s.commit()

    # family_reminders: structural prefix
    with sf() as s:
        rows = s.execute(
            select(FamilyReminder)
            .where(FamilyReminder.embedding_json.is_(None))
        ).scalars().all()
        log.info("family_reminders NULL embeddings: %d", len(rows))
        for rem in rows:
            text = f"Напоминание: {rem.title}"
            try:
                vec = ec.embed_document(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed failed for reminder=%s: %s", rem.id, exc)
                continue
            rem.embedding_json = json.dumps(vec)
            rem.embedding_model = MODEL_NAME
            embedded_reminders += 1
            if embedded_reminders % 10 == 0:
                s.commit()
                log.info("reminders: %d/%d embedded", embedded_reminders, len(rows))
            time.sleep(THROTTLE_SECONDS)
        s.commit()

    log.info(
        "DONE checklist_items=%d reminders=%d (model=%s)",
        embedded_items, embedded_reminders, MODEL_NAME,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
