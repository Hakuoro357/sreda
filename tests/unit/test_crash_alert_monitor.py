"""#335 (мера D пост-мортема vex#331) — тесты монитора крэшей ходов.

Машинные пункты чеклиста приёмки:
- всплеск падений на sreda_home виден ОТДЕЛЬНО от sreda (срез по (канал, бот));
- появление ActiveSqlTransaction в логе → детект с первого появления (не постфактум);
- алерт содержит канал + тенант + класс ошибки;
- кулдаун: та же сигнатура не алертится повторно внутри часа;
- первый запуск не алертит историю (offset'ы = EOF).

Часы везде заморожены явной передачей ``now`` (урок g-047).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sreda.db.models.core import InboundMessage, Tenant
from sreda.workers import crash_alert_monitor as cam

_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)


def _mk_inbound(session, i, *, bot_key, status="ingested", age_min=15,
                tenant_id=None, channel="telegram"):
    session.add(InboundMessage(
        id=f"inb_ca_{i}", tenant_id=tenant_id, channel_type=channel, bot_key=bot_key,
        processing_status=status, status="accepted",
        created_at=_NOW - timedelta(minutes=age_min),
    ))


@pytest.fixture()
def _tenant(db_session):
    t = Tenant(id="tenant_ca_1", name="ca-test")
    db_session.add(t)
    db_session.commit()
    return t


class TestScanStuckInbound:
    def test_spike_on_sreda_home_visible_separately(self, db_session, _tenant):
        """Чеклист: всплеск на sreda_home виден отдельно от sreda."""
        for i in range(3):
            _mk_inbound(db_session, i, bot_key="sreda_home", tenant_id=_tenant.id)
        _mk_inbound(db_session, 10, bot_key="sreda", status="processed")
        db_session.commit()

        slices = cam.scan_stuck_inbound(db_session, _NOW, threshold=2)
        assert len(slices) == 1
        s = slices[0]
        assert (s.channel_type, s.bot_key, s.count) == ("telegram", "sreda_home", 3)
        assert "tenant_ca_1" in s.tenants

    def test_threshold_and_grace_and_window(self, db_session, _tenant):
        """Ниже порога / моложе grace / старше окна — не срез."""
        _mk_inbound(db_session, 0, bot_key="sreda_home", tenant_id=_tenant.id)          # 1 < порога 2
        _mk_inbound(db_session, 1, bot_key="sreda", age_min=2, tenant_id=_tenant.id)     # моложе grace 10м
        _mk_inbound(db_session, 2, bot_key="sreda", age_min=45, tenant_id=_tenant.id)    # старше окна 30м
        db_session.commit()
        assert cam.scan_stuck_inbound(db_session, _NOW, threshold=2) == []

    def test_processed_and_ignored_not_counted(self, db_session, _tenant):
        for i, st in enumerate(("processed", "ignored")):
            _mk_inbound(db_session, i, bot_key="sreda_home", status=st, tenant_id=_tenant.id)
        db_session.commit()
        assert cam.scan_stuck_inbound(db_session, _NOW, threshold=1) == []


class TestScanLogs:
    def test_active_sql_transaction_detected_from_first_occurrence(self, tmp_path):
        """Чеклист: ActiveSqlTransaction в логе → детект (класс по имени)."""
        log = tmp_path / "uvicorn.log"
        log.write_text("старая строка\n", encoding="utf-8")
        state = cam._State(initialized=True, offsets={str(log): log.stat().st_size})

        with log.open("a", encoding="utf-8") as f:
            f.write("psycopg.errors.ActiveSqlTransaction: SET TRANSACTION ...\n")
            f.write("ERROR ... background turn processing crashed\n")

        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"uvicorn.log": 1}
        assert hits.get(cam._TURN_CRASH_CLASS) == {"uvicorn.log": 1}

    def test_first_run_initializes_to_eof_and_stays_silent(self, tmp_path):
        """Чеклист/анти-шум: первый запуск не алертит историю."""
        log = tmp_path / "poller.log"
        log.write_text("psycopg.errors.ActiveSqlTransaction: историческая\n", encoding="utf-8")
        state = cam._State()  # initialized=False

        assert cam.scan_logs(state, str(tmp_path / "*.log")) == {}
        assert state.initialized is True
        # а НОВАЯ строка после инициализации — детектится
        with log.open("a", encoding="utf-8") as f:
            f.write("psycopg.errors.ActiveSqlTransaction: свежая\n")
        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"poller.log": 1}

    def test_rotation_resets_offset(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("x" * 100, encoding="utf-8")
        state = cam._State(initialized=True, offsets={str(log): 100})
        log.write_text("psycopg.errors.InFailedSqlTransaction: после ротации\n", encoding="utf-8")

        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("InFailedSqlTransaction") == {"app.log": 1}


class TestTickAndCooldown:
    def _run_tick(self, monkeypatch, db_session, state, tmp_path, now):
        """tick_once с подменёнными сессией/алертом (monkeypatch модульных символов)."""
        sent: list[str] = []

        class _Ctx:
            def __enter__(self):
                return db_session

            def __exit__(self, *a):  # noqa: ANN002
                return False

        monkeypatch.setattr(cam, "privileged_session", lambda *_a, **_k: _Ctx())
        monkeypatch.setattr(cam, "send_admin_alert", lambda text: sent.append(text))
        monkeypatch.setattr(cam._State, "save", lambda self: None)
        text = cam.tick_once(state, now=now, log_glob=str(tmp_path / "*.log"))
        return text, sent

    def test_alert_contains_channel_tenant_and_class(self, monkeypatch, db_session, _tenant, tmp_path):
        """Чеклист: алерт содержит канал + тенант + класс ошибки."""
        for i in range(2):
            _mk_inbound(db_session, i, bot_key="sreda_home", tenant_id=_tenant.id)
        db_session.commit()
        log = tmp_path / "uvicorn.log"
        log.write_text("", encoding="utf-8")
        state = cam._State(initialized=True, offsets={str(log): 0})
        log.write_text("psycopg.errors.ActiveSqlTransaction: boom\n", encoding="utf-8")

        text, sent = self._run_tick(monkeypatch, db_session, state, tmp_path, _NOW)
        assert text is not None and sent == [text]
        assert "telegram/sreda_home" in text          # канал + бот
        assert "tenant_ca_1" in text                  # тенант
        assert "ActiveSqlTransaction" in text         # класс ошибки

    def test_cooldown_suppresses_repeat_alert(self, monkeypatch, db_session, _tenant, tmp_path):
        """Кулдаун: та же сигнатура не алертится второй раз внутри часа, алертится после."""
        for i in range(2):
            _mk_inbound(db_session, i, bot_key="sreda_home", tenant_id=_tenant.id)
        db_session.commit()
        state = cam._State(initialized=True)

        text1, _ = self._run_tick(monkeypatch, db_session, state, tmp_path, _NOW)
        assert text1 is not None
        text2, _ = self._run_tick(monkeypatch, db_session, state, tmp_path,
                                  _NOW + timedelta(minutes=5))
        assert text2 is None  # сигнатура db:telegram:sreda_home остывает
        # инцидент живёт: НОВЫЕ зависшие входящие продолжают копиться
        t3 = _NOW + timedelta(minutes=61)
        for i in range(2):
            db_session.add(InboundMessage(
                id=f"inb_ca_late_{i}", tenant_id=_tenant.id, channel_type="telegram",
                bot_key="sreda_home", processing_status="ingested", status="accepted",
                created_at=t3 - timedelta(minutes=15),
            ))
        db_session.commit()
        text3, _ = self._run_tick(monkeypatch, db_session, state, tmp_path, t3)
        assert text3 is not None  # кулдаун прошёл — алертим снова
