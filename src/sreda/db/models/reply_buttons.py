"""Reply-button cache — inline-кнопки LLM-ответов.

Когда LLM вызывает ``reply_with_buttons(text, buttons)``, каждая
кнопка-метка получает короткий токен (8 hex). Токен идёт в
``callback_data="btn_reply:<token>"``, юзер жмёт — callback-handler
достаёт label, инжектит в payload как обычный text-message, и следующий
chat-turn обрабатывается нормально.

TTL 1 час: токены старше часа игнорируются (старые кнопки «протухли»).
Удалять сразу не нужно — воркер подметёт раз в сутки. До этого
``resolve_token`` проверяет возраст и отказывает в устаревших.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class ReplyButtonCache(Base):
    __tablename__ = "reply_button_cache"

    # 8 hex-символов (32 бита). С TTL=1h и <100 запросами/мин на тенанта
    # коллизии пренебрежимо редки. Короткий ключ чтобы влезало в
    # Telegram callback_data (64 байта) с префиксом "btn_reply:".
    token: Mapped[str] = mapped_column(String(16), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Сам текст кнопки (≤20 симв по prompt-правилу, 128 на всякий случай).
    # Одновременно — то, что подставится как message_text в следующий turn.
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    # Отмечаем когда токен уже был использован — защита от double-tap
    # (юзер кликнул, потом снова открыл чат и кликнул ту же кнопку).
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
