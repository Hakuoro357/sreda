"""#335 (мера D пост-мортема vex#331) — тесты монитора крэшей ходов.

Машинные пункты чеклиста приёмки:
- всплеск падений на sreda_home виден ОТДЕЛЬНО от sreda (срез по (канал, бот));
- появление ActiveSqlTransaction в логе → детект с первого появления (не постфактум);
- алерт содержит канал + тенант + класс ошибки;
- первый запуск не алертит историю (офсеты = EOF).

Плюс R1-ревью: НАСТОЯЩАЯ сигнатура send_admin_alert (заглушка с полным контрактом — R1 CRITICAL:
одноаргументная lambda замаскировала бы TypeError), copytruncate-ротация по отпечатку головы,
маркер на границе кусков (carry), офсет не двигается при сбое чтения, ярусы прочих psycopg-классов,
MAX-маркер. Часы заморожены явной передачей ``now`` (урок g-047).
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


@pytest.fixture()
def alert_spy(monkeypatch):
    """Заглушка send_admin_alert С НАСТОЯЩИМ контрактом (severity, title, body, *, dedupe_key, ...).

    R1 CRITICAL: одноаргументная lambda скрыла бы TypeError продового вызова. Здесь неверный вызов
    монитора упадёт прямо в тесте.
    """
    calls: list[dict] = []

    def _spy(severity, title, body, *, dedupe_key=None, extra_context=None, both_channels=False):
        assert severity in ("P0", "P1", "P2", "INFO")
        calls.append({"severity": severity, "title": title, "body": body,
                      "dedupe_key": dedupe_key})

    monkeypatch.setattr(cam, "send_admin_alert", _spy)
    return calls


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
    def _init_state(self, *paths) -> cam._State:
        """State как после первого прогона: off=EOF, отпечаток головы с зафиксированной длиной."""
        import os
        st = cam._State(initialized=True)
        for p in paths:
            size = os.path.getsize(p)
            n = min(cam._HEAD_SIG_BYTES, size)
            st.files[str(p)] = {"off": size, "sig": cam._head_sig(str(p), n), "sig_len": n,
                                "carry": ""}
        return st

    def test_active_sql_transaction_detected_from_first_occurrence(self, tmp_path):
        """Чеклист: ActiveSqlTransaction и turn-crash маркеры (оба канала) детектятся сразу."""
        log = tmp_path / "uvicorn.log"
        log.write_text("старая строка\n", encoding="utf-8")
        state = self._init_state(log)

        with log.open("a", encoding="utf-8") as f:
            f.write("psycopg.errors.ActiveSqlTransaction: SET TRANSACTION ...\n")
            f.write("ERROR ... background turn processing crashed\n")
            f.write("ERROR ... max approved turn crashed: tenant=x inbound=y\n")

        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"uvicorn.log": 1}
        assert hits.get("turn_crash(telegram)") == {"uvicorn.log": 1}
        assert hits.get("turn_crash(max)") == {"uvicorn.log": 1}

    def test_first_run_initializes_to_eof_and_stays_silent(self, tmp_path):
        """Анти-шум: первый запуск не алертит историю; новая строка после — детектится."""
        log = tmp_path / "poller.log"
        log.write_text("psycopg.errors.ActiveSqlTransaction: историческая\n", encoding="utf-8")
        state = cam._State()  # initialized=False

        assert cam.scan_logs(state, str(tmp_path / "*.log")) == {}
        assert state.initialized is True
        with log.open("a", encoding="utf-8") as f:
            f.write("psycopg.errors.ActiveSqlTransaction: свежая\n")
        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"poller.log": 1}

    def test_new_file_after_init_read_from_start(self, tmp_path):
        """R1 MAJOR: файл, появившийся ПОСЛЕ инициализации, читается с нуля (rename-ротация)."""
        state = cam._State(initialized=True)
        log = tmp_path / "fresh.log"
        log.write_text("psycopg.errors.ActiveSqlTransaction: в новом файле\n", encoding="utf-8")

        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"fresh.log": 1}

    def test_copytruncate_detected_by_head_signature(self, tmp_path):
        """R1 MAJOR: copytruncate + файл отрос ВЫШЕ старого офсета — ловим по отпечатку головы."""
        log = tmp_path / "app.log"
        log.write_text("A" * 500, encoding="utf-8")
        state = self._init_state(log)  # off=500
        # truncate → новое содержимое БОЛЬШЕ старого офсета (size<off не сработает)
        log.write_text("B" * 400 + "psycopg.errors.ActiveSqlTransaction: после truncate\n" + "C" * 200,
                       encoding="utf-8")

        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"app.log": 1}

    def test_marker_split_across_chunks_caught_by_carry(self, tmp_path, monkeypatch):
        """R1 MINOR: маркер, разрезанный границей кусков, ловится carry-хвостом (и не двоится)."""
        log = tmp_path / "seam.log"
        log.write_text("", encoding="utf-8")
        state = self._init_state(log)
        with log.open("a", encoding="utf-8") as f:
            f.write("psycopg.err")  # первая половина маркера
        assert cam.scan_logs(state, str(tmp_path / "*.log")) == {}
        with log.open("a", encoding="utf-8") as f:
            f.write("ors.ActiveSqlTransaction: раскол\n")
        hits = cam.scan_logs(state, str(tmp_path / "*.log"))
        assert hits.get("ActiveSqlTransaction") == {"seam.log": 1}
        # третий тик без новых строк — ничего (carry не даёт двойной счёт)
        assert cam.scan_logs(state, str(tmp_path / "*.log")) == {}

    def test_offset_advances_only_by_read_bytes_with_cap(self, tmp_path, monkeypatch):
        """R1 MAJOR: кусок больше потолка — офсет двигается на прочитанное, остаток не теряется."""
        monkeypatch.setattr(cam, "_MAX_CHUNK_BYTES", 64)
        log = tmp_path / "big.log"
        log.write_text("", encoding="utf-8")
        state = self._init_state(log)
        with log.open("a", encoding="utf-8") as f:
            f.write("x" * 100 + "psycopg.errors.ActiveSqlTransaction: за потолком\n")
        # дочитываем потолочными кусками: маркер в итоге найден ПОЛНЫМ именем ровно один раз
        # (матч, упёршийся в обрезанный хвост куска, бросается — иначе посчитали бы "ActiveSqlTr")
        merged: dict[str, int] = {}
        for _ in range(5):
            for cls, files in cam.scan_logs(state, str(tmp_path / "*.log")).items():
                merged[cls] = merged.get(cls, 0) + sum(files.values())
        assert merged == {"ActiveSqlTransaction": 1}


class TestTiersAndTick:
    def test_other_psycopg_classes_tiered_info_with_min_count(self):
        """R1 MAJOR (ложные P2): прочие psycopg-классы — INFO и только при >=3; порча — P2 с 1."""
        tiers = cam._classify_log_hits({
            "ActiveSqlTransaction": {"a.log": 1},
            "UniqueViolation": {"a.log": 2},          # 2 < 3 → молчим
            "LockNotAvailable": {"a.log": 2, "b.log": 1},  # 3 → INFO
            "turn_crash(telegram)": {"a.log": 1},
        })
        d = {cls: sev for sev, cls, _ in tiers}
        assert d["ActiveSqlTransaction"] == "P2"
        assert d["turn_crash(telegram)"] == "P2"
        assert d["LockNotAvailable"] == "INFO"
        assert "UniqueViolation" not in d

    def _run_tick(self, monkeypatch, db_session, state, tmp_path, now):
        class _Ctx:
            def __enter__(self):
                return db_session

            def __exit__(self, *a):  # noqa: ANN002
                return False

        monkeypatch.setattr(cam, "privileged_session", lambda *_a, **_k: _Ctx())
        monkeypatch.setattr(cam._State, "save", lambda self: None)
        return cam.tick_once(state, now=now, log_glob=str(tmp_path / "*.log"))

    def test_alert_contains_channel_tenant_and_class(self, monkeypatch, db_session, _tenant,
                                                     tmp_path, alert_spy):
        """Чеклист: алерты содержат канал + тенант (БД-срез) и класс ошибки (лог), P2, свои ключи."""
        for i in range(2):
            _mk_inbound(db_session, i, bot_key="sreda_home", tenant_id=_tenant.id)
        db_session.commit()
        log = tmp_path / "uvicorn.log"
        log.write_text("", encoding="utf-8")
        state = cam._State(initialized=True,
                           files={str(log): {"off": 0, "sig": "", "carry": ""}})
        log.write_text("psycopg.errors.ActiveSqlTransaction: boom\n", encoding="utf-8")

        sent = self._run_tick(monkeypatch, db_session, state, tmp_path, _NOW)
        assert sorted(sent) == ["crash_alert:db:telegram:sreda_home",
                                "crash_alert:log:ActiveSqlTransaction"]
        db_alert = next(c for c in alert_spy if c["dedupe_key"].startswith("crash_alert:db:"))
        assert db_alert["severity"] == "P2"
        assert "telegram/sreda_home" in db_alert["title"] + db_alert["body"]  # канал+бот
        assert "tenant_ca_1" in db_alert["body"]                              # тенант
        log_alert = next(c for c in alert_spy if c["dedupe_key"].startswith("crash_alert:log:"))
        assert log_alert["severity"] == "P2"
        assert "ActiveSqlTransaction" in log_alert["title"]                   # класс ошибки

    def test_quiet_when_nothing_wrong(self, monkeypatch, db_session, _tenant, tmp_path, alert_spy):
        _mk_inbound(db_session, 0, bot_key="sreda_home", status="processed", tenant_id=_tenant.id)
        db_session.commit()
        state = cam._State(initialized=True)
        sent = self._run_tick(monkeypatch, db_session, state, tmp_path, _NOW)
        assert sent == [] and alert_spy == []

    def test_max_channel_slice_visible(self, db_session, _tenant):
        """R1: MAX-канал — отдельный срез (channel_type='max'), не сливается с telegram."""
        for i in range(2):
            _mk_inbound(db_session, i, bot_key="sreda", channel="max", tenant_id=_tenant.id)
        db_session.commit()
        slices = cam.scan_stuck_inbound(db_session, _NOW, threshold=2)
        assert [(s.channel_type, s.bot_key) for s in slices] == [("max", "sreda")]


class TestStateAndFlag:
    def test_state_roundtrip_through_real_runtime_config(self, monkeypatch, db_session):
        """R1 субагент: save/load гоняем через НАСТОЯЩИЙ runtime_config (не подменённый)."""
        class _Ctx:
            def __enter__(self):
                return db_session

            def __exit__(self, *a):  # noqa: ANN002
                return False

        monkeypatch.setattr(cam, "privileged_session", lambda *_a, **_k: _Ctx())
        st = cam._State(initialized=True,
                        files={"/var/log/sreda/x.log": {"off": 42, "sig": "abc", "sig_len": 3,
                                                        "carry": "хвост"}})
        st.save()
        loaded = cam._State.load()
        assert loaded.initialized is True
        assert loaded.files["/var/log/sreda/x.log"]["off"] == 42
        assert loaded.files["/var/log/sreda/x.log"]["carry"] == "хвост"

    def test_corrupted_state_starts_fresh(self, monkeypatch, db_session):
        class _Ctx:
            def __enter__(self):
                return db_session

            def __exit__(self, *a):  # noqa: ANN002
                return False

        monkeypatch.setattr(cam, "privileged_session", lambda *_a, **_k: _Ctx())
        from sreda.services import runtime_config as rc
        rc.set_config(db_session, cam._RC_STATE_KEY, "{битый json")
        rc.invalidate_cache()
        loaded = cam._State.load()
        assert loaded.files == {} and loaded.initialized is False

    @pytest.mark.asyncio
    async def test_disabled_flag_exits_loop(self, monkeypatch):
        monkeypatch.setenv("SREDA_CRASH_ALERT_ENABLED", "0")
        loaded = []
        monkeypatch.setattr(cam._State, "load", classmethod(lambda cls: loaded.append(1)))
        await cam.run_crash_alert_loop()  # выключен → выходит сразу, state не грузит
        assert loaded == []
