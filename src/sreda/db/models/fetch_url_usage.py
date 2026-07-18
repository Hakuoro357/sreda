"""FetchUrlUsage — per-(tenant, user, ymd) счётчик вызовов `fetch_url` (#244).

В отличие от WebSearchUsage (Tavily, per-МЕСЯЦ, общий API-пул) — у fetch_url нет метрированного внешнего API:
лимит чисто АНТИ-ЗЛОУПОТРЕБЛЕНИЕ (cost/egress-exhaustion через публичный бот). Поэтому период — per-ДЕНЬ
(`ymd` = "YYYY-MM-DD" UTC), один счётчик `fetch_calls`, без global cap.

NO-REFUND (план #244, выбор владельца): резерв через атомарный `try_consume_fetch_url` ТОЛЬКО после успешного
pre-flight (socksio+proxy+TCP-probe PORT2); расход безвозвратен → компенсирующего decrement НЕТ (в отличие от
`release_tavily`). Это снимает весь класс ошибок refund-классификации (ProxyError vs ConnectError-post-TLS).

Mirror WebSearchUsage: композитный UNIQUE на тройку для атомарного INSERT…ON CONFLICT; дублируем Index в модели,
чтобы `create_all` в тестах (SQLite) дал тот же констрейнт, что миграция 20260630_0073 на проде.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class FetchUrlUsage(Base):
    __tablename__ = "fetch_url_usage"

    __table_args__ = (
        Index(
            "ix_fetch_url_usage_unique",
            "tenant_id", "user_id", "ymd",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ymd: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD UTC
    fetch_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
