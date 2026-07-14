"""#344 F5 — bounded callback row-lock (Opus-адверсар MAJOR#2).

Оба ack/snooze-обработчика (TG ``telegram_bot._handle_reminder_callback`` и MAX
``max_inbound._handle_max_reminder_callback``) живут в UVICORN event-loop и берут
строку ``FamilyReminder`` под ``SELECT ... FOR UPDATE`` (см. cross-process гонку в
``test_344_delivery_contour_pg``). Синхронный ``FOR UPDATE`` — блокирующий
lock-wait: пока reminder-воркер (job_runner) держит лок строки, весь event-loop
(все тенанты процесса) стоит. Без границы медленный тик воркера подвешивает loop.

Фикс (``read_reminder_for_callback``): PG-only ``SET LOCAL lock_timeout`` перед
``FOR UPDATE`` → fail-fast (``ReminderLockTimeout``, sqlstate 55P03) вместо
неограниченного ожидания; обработчик показывает дружелюбный «попробуйте ещё раз»
тост (БЕЗ технических деталей) и не трогает напоминание. SQLite (unit) — без
лока/таймаута, plain read с ``populate_existing`` (свежесть явная).

Эти тесты детерминированны (fakes/mocks), PG не нужен; настоящая сериализация
покрыта PG-свитом.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from sreda.services.housewife_reminders import (
    REMINDER_CALLBACK_BUSY_TEXT,
    REMINDER_CALLBACK_LOCK_TIMEOUT_MS,
    ReminderLockTimeout,
    read_reminder_for_callback,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeOrig(Exception):
    """DBAPI-error stand-in carrying an sqlstate (psycopg3 ``.sqlstate``)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _op_error(sqlstate: str) -> OperationalError:
    return OperationalError("SELECT ... FOR UPDATE", {}, _FakeOrig(sqlstate))


class _FakeSession:
    def __init__(self, *, dialect: str, get_result=None, get_exc=None) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self._get_result = get_result
        self._get_exc = get_exc
        self.executed: list[str] = []
        self.rolled_back = False
        self.get_kwargs: dict | None = None
        self.get_called = False

    def execute(self, statement, params=None):  # noqa: ANN001
        self.executed.append(str(statement))
        return None

    def get(self, entity, ident, **kwargs):  # noqa: ANN001
        self.get_called = True
        self.get_kwargs = kwargs
        if self._get_exc is not None:
            raise self._get_exc
        return self._get_result

    def rollback(self) -> None:
        self.rolled_back = True


# --------------------------------------------------------------------------
# read_reminder_for_callback — lock_timeout mechanism
# --------------------------------------------------------------------------


def test_lock_timeout_raises_reminder_lock_timeout_and_rolls_back():
    """PG + ``FOR UPDATE`` таймаут (sqlstate 55P03) → ``ReminderLockTimeout``,
    сессия откачена (не оставляем висящую транзакцию с таймаут-настройкой)."""
    session = _FakeSession(dialect="postgresql", get_exc=_op_error("55P03"))
    with pytest.raises(ReminderLockTimeout):
        read_reminder_for_callback(session, "rem-1")
    assert session.rolled_back is True


def test_non_lock_operational_error_propagates_unchanged():
    """Другой ``OperationalError`` (напр. serialization 40001) НЕ маскируется под
    lock-timeout — пробрасывается как есть, обработчик его не глотает."""
    session = _FakeSession(dialect="postgresql", get_exc=_op_error("40001"))
    with pytest.raises(OperationalError):
        read_reminder_for_callback(session, "rem-1")
    assert session.rolled_back is False


def test_pg_sets_lock_timeout_before_for_update():
    """PG happy-path: перед ``FOR UPDATE`` выставлен ``SET LOCAL lock_timeout``
    на сконфигурированное значение; читаем с ``with_for_update`` + свежесть."""
    sentinel = object()
    session = _FakeSession(dialect="postgresql", get_result=sentinel)
    out = read_reminder_for_callback(session, "rem-1")
    assert out is sentinel
    assert any("lock_timeout" in s.lower() for s in session.executed)
    assert str(REMINDER_CALLBACK_LOCK_TIMEOUT_MS) in " ".join(session.executed)
    assert session.get_kwargs is not None
    assert session.get_kwargs.get("with_for_update") == {"key_share": False}
    assert session.get_kwargs.get("populate_existing") is True


def test_sqlite_plain_read_no_lock_no_timeout():
    """SQLite (unit): ни ``SET LOCAL``, ни ``FOR UPDATE``; свежий read
    (``populate_existing``), чтобы freshness-контракт был явным."""
    sentinel = object()
    session = _FakeSession(dialect="sqlite", get_result=sentinel)
    out = read_reminder_for_callback(session, "rem-1")
    assert out is sentinel
    assert session.executed == []  # никаких SET LOCAL
    assert session.get_kwargs is not None
    assert "with_for_update" not in session.get_kwargs
    assert session.get_kwargs.get("populate_existing") is True


def test_empty_reminder_id_returns_none_without_query():
    session = _FakeSession(dialect="postgresql")
    assert read_reminder_for_callback(session, "") is None
    assert session.get_called is False
    assert session.executed == []


def test_busy_text_has_no_technical_details():
    """Правило «никаких технических данных юзеру»: retry-текст без латиницы,
    кодов ошибок, слов lock/timeout/error."""
    txt = REMINDER_CALLBACK_BUSY_TEXT
    assert not re.search(r"[A-Za-z]", txt), txt
    for bad in ("55P03", "lock", "timeout", "error"):
        assert bad.lower() not in txt.lower()


# --------------------------------------------------------------------------
# Callback handlers — fail-fast retry toast on ReminderLockTimeout
# --------------------------------------------------------------------------


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.answered: list[str] = []
        self.edited: list[str] = []

    async def answer_callback_query(self, callback_id, text=None, **kw):  # noqa: ANN001
        self.answered.append(str(text))
        return {"ok": True}

    async def edit_message_text(self, **kw):  # noqa: ANN001
        self.edited.append("edited")
        return {"ok": True}


class _FakeMaxClient:
    def __init__(self) -> None:
        self.notifications: list[str] = []
        self.messages: list[dict] = []

    async def answer_callback(self, callback_id, *, notification=None, message=None):  # noqa: ANN001
        if notification is not None:
            self.notifications.append(str(notification))
        if message is not None:
            self.messages.append(message)
        return {"ok": True}


class _ExplodingSession:
    """commit() must NOT be reached on lock-timeout."""

    def commit(self):
        raise AssertionError("session.commit() must not run on lock-timeout")

    def rollback(self):
        pass


@pytest.mark.asyncio
async def test_tg_callback_lock_timeout_shows_retry_toast(monkeypatch):
    import sreda.services.housewife_reminders as _hr
    from sreda.services.telegram_bot import _handle_reminder_callback

    def _raise(*a, **k):
        raise ReminderLockTimeout

    monkeypatch.setattr(_hr, "read_reminder_for_callback", _raise)
    fake = _FakeTelegramClient()
    await _handle_reminder_callback(
        session=_ExplodingSession(),
        telegram_client=fake,
        callback_query={
            "id": "cb1",
            "message": {"chat": {"id": "1"}, "message_id": 5, "text": "🔔 X"},
        },
        data="rem_snooze:rid-1",
    )
    assert fake.answered == [REMINDER_CALLBACK_BUSY_TEXT]
    assert fake.edited == []  # кнопки не трогаем — юзер перетапнет


@pytest.mark.asyncio
async def test_max_callback_lock_timeout_shows_visible_retry_with_buttons(monkeypatch):
    """MAX глушит notification в DM (probe хендлера) → retry должен быть ВИДИМЫМ
    message-replacement, СОХРАНЯЯ rem_done/rem_snooze кнопки (Codex sol+terra R1)."""
    import sreda.services.housewife_reminders as _hr
    from sreda.services.max_inbound import _handle_max_reminder_callback

    def _raise(*a, **k):
        raise ReminderLockTimeout

    monkeypatch.setattr(_hr, "read_reminder_for_callback", _raise)
    fake = _FakeMaxClient()
    await _handle_max_reminder_callback(
        session=_ExplodingSession(),
        max_client=fake,
        callback_id="cb1",
        data="rem_snooze:rid-1",
        payload={"message": {"body": {"text": "🔔 Полить цветы"}}},
        onboarding=SimpleNamespace(tenant_id="t1"),
    )
    # notification НЕ используем (в MAX DM невидим); используем видимый message.
    assert fake.notifications == []
    assert len(fake.messages) == 1
    msg = fake.messages[0]
    assert REMINDER_CALLBACK_BUSY_TEXT in msg["text"]
    # Кнопки сохранены → юзер перетапнет (attachments не пустые, callback rem_*).
    attachments = msg.get("attachments")
    assert attachments, "retry-сообщение должно сохранить кнопки для повтора"
    _blob = str(attachments)
    assert "rem_done:rid-1" in _blob and "rem_snooze:rid-1" in _blob


@pytest.mark.asyncio
async def test_max_retry_text_does_not_accumulate_on_repeated_timeout(monkeypatch):
    """timeout→timeout: тело уже несёт retry-строку от прошлого таймаута; новый
    replacement НЕ должен дублировать её (Codex sol+terra R2 MINOR)."""
    import sreda.services.housewife_reminders as _hr
    from sreda.services.max_inbound import _handle_max_reminder_callback

    def _raise(*a, **k):
        raise ReminderLockTimeout

    monkeypatch.setattr(_hr, "read_reminder_for_callback", _raise)
    fake = _FakeMaxClient()
    # Тело, как его оставил ПРЕДЫДУЩИЙ lock-timeout replacement.
    prior_body = f"🔔 Полить цветы\n{REMINDER_CALLBACK_BUSY_TEXT}"
    await _handle_max_reminder_callback(
        session=_ExplodingSession(),
        max_client=fake,
        callback_id="cb1",
        data="rem_snooze:rid-1",
        payload={"message": {"body": {"text": prior_body}}},
        onboarding=SimpleNamespace(tenant_id="t1"),
    )
    text = fake.messages[0]["text"]
    assert text.count(REMINDER_CALLBACK_BUSY_TEXT) == 1, text
    assert "Полить цветы" in text  # заголовок сохранён, не потерян


@pytest.mark.asyncio
async def test_max_retry_text_does_not_leak_into_success_reply(monkeypatch):
    """timeout→success: тело несёт retry-строку от прошлого таймаута; финальный
    ✅-текст успешного ack НЕ должен её содержать (Codex sol+terra R2 MINOR)."""
    import sreda.services.housewife_reminders as _hr
    from sreda.services.max_inbound import _handle_max_reminder_callback

    reminder = SimpleNamespace(id="rid-1", tenant_id="t1")
    monkeypatch.setattr(
        _hr, "read_reminder_for_callback", lambda *a, **k: reminder
    )

    class _FakeService:
        def __init__(self, session):  # noqa: ANN001
            pass

        def acknowledge(self, rem):  # noqa: ANN001
            pass

        def snooze(self, rem, minutes=0):  # noqa: ANN001
            pass

    monkeypatch.setattr(_hr, "HousewifeReminderService", _FakeService)

    class _CommitSession:
        def commit(self):
            pass

        def rollback(self):
            pass

    fake = _FakeMaxClient()
    prior_body = f"🔔 Полить цветы\n{REMINDER_CALLBACK_BUSY_TEXT}"
    await _handle_max_reminder_callback(
        session=_CommitSession(),
        max_client=fake,
        callback_id="cb1",
        data="rem_done:rid-1",
        payload={"message": {"body": {"text": prior_body}}},
        onboarding=SimpleNamespace(tenant_id="t1"),
    )
    text = fake.messages[0]["text"]
    assert REMINDER_CALLBACK_BUSY_TEXT not in text, text
    assert "✅" in text and "Полить цветы" in text


# --------------------------------------------------------------------------
# R3 (Codex sol+terra MINOR): retry-строка снимается ТОЛЬКО как отдельная
# trailing-строка (разделитель \n / \r\n — как её и генерирует хендлер), НЕ
# любой суффикс. Иначе легитимный заголовок, содержащий/равный фразе, терялся бы.
# Эти три RED-теста краснели на прежней ``endswith(BUSY_TEXT)``-логике.
# --------------------------------------------------------------------------


class _FakeReminderService:
    def __init__(self, session):  # noqa: ANN001
        pass

    def acknowledge(self, rem):  # noqa: ANN001
        pass

    def snooze(self, rem, minutes=0):  # noqa: ANN001
        pass


class _CommitOnlySession:
    def commit(self):
        pass

    def rollback(self):
        pass


async def _run_max_success_ack(monkeypatch, *, body_text, action="rem_done"):
    """Прогнать успешный ack/snooze MAX-callback с заданным телом сообщения;
    вернуть текст финального replacement (``✅ …`` / ``⏰ …``)."""
    import sreda.services.housewife_reminders as _hr
    from sreda.services.max_inbound import _handle_max_reminder_callback

    reminder = SimpleNamespace(id="rid-1", tenant_id="t1")
    monkeypatch.setattr(_hr, "read_reminder_for_callback", lambda *a, **k: reminder)
    monkeypatch.setattr(_hr, "HousewifeReminderService", _FakeReminderService)
    fake = _FakeMaxClient()
    await _handle_max_reminder_callback(
        session=_CommitOnlySession(),
        max_client=fake,
        callback_id="cb1",
        data=f"{action}:rid-1",
        payload={"message": {"body": {"text": body_text}}},
        onboarding=SimpleNamespace(tenant_id="t1"),
    )
    return fake.messages[0]["text"]


@pytest.mark.asyncio
async def test_max_retry_strip_preserves_title_ending_with_phrase_same_line(monkeypatch):
    """Заголовок, ОКАНЧИВАЮЩИЙСЯ фразой на ТОЙ ЖЕ строке (без переноса перед ней),
    НЕ обрезается — это была бы потеря пользовательских данных. RED на прежней
    ``endswith``-логике (обрезала до ``Записать:``)."""
    title = f"Записать: {REMINDER_CALLBACK_BUSY_TEXT}"
    text = await _run_max_success_ack(monkeypatch, body_text=f"🔔 {title}")
    assert text == f"✅ {title}", text


@pytest.mark.asyncio
async def test_max_retry_strip_handles_crlf_delimited_retry_line(monkeypatch):
    """Retry-строка с CRLF-разделителем (``\\r\\n``) снимается ЧИСТО — без висящего
    ``\\r`` и без остатка фразы; заголовок сохранён. RED на прежней логике
    (``rstrip('\\n \\t')`` не снимала ``\\r`` → оставался хвост ``Полить цветы\\r``)."""
    body = f"🔔 Полить цветы\r\n{REMINDER_CALLBACK_BUSY_TEXT}"
    text = await _run_max_success_ack(monkeypatch, body_text=body)
    assert text == "✅ Полить цветы", text
    assert "\r" not in text
    assert REMINDER_CALLBACK_BUSY_TEXT not in text


@pytest.mark.asyncio
async def test_max_retry_strip_preserves_title_exactly_equal_to_phrase(monkeypatch):
    """Заголовок, РАВНЫЙ фразе целиком (без разделителя-переноса), НЕ снимается:
    отличить его от нашего инъектированного bare-retry нельзя, и мы выбираем НЕ
    терять данные пользователя (принятый trade-off — decision-log ACKRACE-2).
    RED на прежней логике (обрезала в пустоту → generic ``✅ Готово``)."""
    text = await _run_max_success_ack(
        monkeypatch, body_text=f"🔔 {REMINDER_CALLBACK_BUSY_TEXT}"
    )
    assert text == f"✅ {REMINDER_CALLBACK_BUSY_TEXT}", text
