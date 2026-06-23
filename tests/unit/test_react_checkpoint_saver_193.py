"""#193 Phase A — golden-контракт EncryptedSqlCheckpointSaver (вариант B).

Паритет с InMemorySaver + специфика Среды: полное шифрование payload (raw blob = ciphertext),
знаковый idx + дедуп put_writes, clear_pending (idx<0), decrypt-fail → None (свежий старт),
переживание нового инстанса saver'а (durable «рестарт»), list before/limit + filter unsupported.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.runtime.react_checkpoint_saver import EncryptedSqlCheckpointSaver


@pytest.fixture()
def db():
    """Файловый sqlite-движок с 2 нашими таблицами + factory."""
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SF, engine
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def saver(db):
    SF, _ = db
    return EncryptedSqlCheckpointSaver(session_factory=SF)


def _cfg(thread="react:t1:hmacX", ns="react-v1", cp_id=None):
    conf = {"thread_id": thread, "checkpoint_ns": ns}
    if cp_id is not None:
        conf["checkpoint_id"] = cp_id
    return {"configurable": conf}


def _ckpt(cp_id, text_content="напомни купить молоко"):
    ck = empty_checkpoint()
    ck["id"] = cp_id
    ck["channel_values"] = {"messages": [HumanMessage(content=text_content)]}
    return ck


# --- roundtrip / config / parent ------------------------------------------

def test_put_get_roundtrip_and_config(saver):
    cfg = _cfg()
    ck = _ckpt("00000000-0000-0000-0000-000000000001")
    out = saver.put(cfg, ck, {"source": "loop", "step": 1}, {})
    assert out["configurable"]["checkpoint_id"] == ck["id"]

    got = saver.get_tuple(_cfg())  # без checkpoint_id → последний
    assert got is not None
    assert got.checkpoint["id"] == ck["id"]
    msgs = got.checkpoint["channel_values"]["messages"]
    assert msgs[0].content == "напомни купить молоко"  # исходный текст восстановлен
    assert got.metadata["step"] == 1
    assert got.config["configurable"]["checkpoint_id"] == ck["id"]


def test_parent_config_chain(saver):
    c1 = _ckpt("00000000-0000-0000-0000-000000000001")
    saver.put(_cfg(), c1, {"step": 1}, {})
    # второй checkpoint с parent = первый (parent берётся из config.checkpoint_id)
    c2 = _ckpt("00000000-0000-0000-0000-000000000002", "и хлеб")
    saver.put(_cfg(cp_id=c1["id"]), c2, {"step": 2}, {})

    got = saver.get_tuple(_cfg())
    assert got.checkpoint["id"] == c2["id"]
    assert got.parent_config["configurable"]["checkpoint_id"] == c1["id"]


def test_survives_new_saver_instance(db):
    """Durable «рестарт»: новый инстанс saver'а на той же БД видит прежнюю историю."""
    SF, _ = db
    s1 = EncryptedSqlCheckpointSaver(session_factory=SF)
    ck = _ckpt("00000000-0000-0000-0000-0000000000aa", "секрет-после-рестарта")
    s1.put(_cfg(), ck, {"step": 1}, {})

    s2 = EncryptedSqlCheckpointSaver(session_factory=SF)  # эмуляция нового процесса
    got = s2.get_tuple(_cfg())
    assert got is not None
    assert got.checkpoint["channel_values"]["messages"][0].content == "секрет-после-рестарта"


# --- writes: signed idx + dedup -------------------------------------------

def test_put_writes_signed_idx_and_dedup(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000010")
    saver.put(_cfg(), ck, {}, {})
    wcfg = _cfg(cp_id=ck["id"])

    # обычный write (idx≥0) + interrupt-write (idx=-3 по WRITES_IDX_MAP)
    saver.put_writes(wcfg, [("messages", "v0"), ("__interrupt__", "пауза1")], task_id="task1")
    got = saver.get_tuple(_cfg())
    by_channel = {ch: val for (_tid, ch, val) in got.pending_writes}
    assert by_channel["messages"] == "v0"
    assert by_channel["__interrupt__"] == "пауза1"

    # повтор: обычный write НЕ перезаписывается (idx≥0 DO NOTHING), interrupt ОБНОВЛЯЕТСЯ (idx<0)
    saver.put_writes(wcfg, [("messages", "v0-NEW"), ("__interrupt__", "пауза2")], task_id="task1")
    got2 = saver.get_tuple(_cfg())
    by_channel2 = {ch: val for (_tid, ch, val) in got2.pending_writes}
    assert by_channel2["messages"] == "v0"  # дедуп: старое значение
    assert by_channel2["__interrupt__"] == "пауза2"  # спец-write обновлён

    # проверим знаковый idx в БД напрямую
    SF = saver._sf
    with SF() as s:
        idxs = sorted(r[0] for r in s.execute(text(
            "select idx from react_checkpoint_write")).all())
    assert -3 in idxs  # __interrupt__
    assert any(i >= 0 for i in idxs)  # обычный


# --- шифрование ------------------------------------------------------------

def test_checkpoint_payload_encrypted_raw(saver):
    secret = "СУПЕР-СЕКРЕТНЫЙ-ТЕКСТ-42"
    ck = _ckpt("00000000-0000-0000-0000-000000000020", secret)
    saver.put(_cfg(), ck, {"note": secret}, {})
    saver.put_writes(_cfg(cp_id=ck["id"]), [("__interrupt__", secret)], task_id="t")

    SF = saver._sf
    with SF() as s:
        cp_blobs = s.execute(text("select blob, metadata from react_checkpoint")).all()
        w_blobs = s.execute(text("select blob from react_checkpoint_write")).all()
    raw = b"".join(b for row in cp_blobs for b in row) + b"".join(r[0] for r in w_blobs)
    assert secret.encode("utf-8") not in raw  # plaintext отсутствует во всех blob
    # но через saver — расшифровывается
    got = saver.get_tuple(_cfg())
    assert got.checkpoint["channel_values"]["messages"][0].content == secret


# --- list ------------------------------------------------------------------

def test_list_before_limit_and_filter(saver):
    ids = [f"00000000-0000-0000-0000-00000000003{i}" for i in range(3)]
    for i, cid in enumerate(ids):
        saver.put(_cfg(cp_id=ids[i - 1] if i else None), _ckpt(cid, f"ход{i}"), {"step": i}, {})

    listed = list(saver.list(_cfg()))
    assert [t.checkpoint["id"] for t in listed] == sorted(ids, reverse=True)  # desc

    limited = list(saver.list(_cfg(), limit=1))
    assert len(limited) == 1 and limited[0].checkpoint["id"] == ids[-1]

    before = list(saver.list(_cfg(), before=_cfg(cp_id=ids[-1])))
    assert ids[-1] not in [t.checkpoint["id"] for t in before]

    with pytest.raises(NotImplementedError):
        list(saver.list(_cfg(), filter={"step": 1}))


# --- decrypt-fail → None ---------------------------------------------------

def test_corrupt_blob_returns_none(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000040")
    saver.put(_cfg(), ck, {}, {})
    SF = saver._sf
    with SF() as s:
        s.execute(text("update react_checkpoint set blob = :b"), {"b": b"\x00garbage"})
        s.commit()
    # битый blob → None (граф стартует свежим), без падения
    assert saver.get_tuple(_cfg()) is None


# --- clear_pending ---------------------------------------------------------

def test_clear_pending_removes_interrupt_keeps_history(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000050")
    saver.put(_cfg(), ck, {}, {})
    saver.put_writes(_cfg(cp_id=ck["id"]),
                     [("messages", "обычный"), ("__interrupt__", "пауза")], task_id="t1")

    removed = saver.clear_pending("react:t1:hmacX", "react-v1")
    assert removed == 1  # удалён только interrupt (idx<0)

    got = saver.get_tuple(_cfg())
    assert got is not None  # checkpoint цел
    channels = {ch for (_t, ch, _v) in got.pending_writes}
    assert "__interrupt__" not in channels  # пауза снята
    assert "messages" in channels  # обычный write сохранён


# --- delete_thread (GC / poison-recovery) ---------------------------------

def test_delete_thread_removes_all(saver):
    c1 = _ckpt("00000000-0000-0000-0000-000000000071")
    saver.put(_cfg(), c1, {}, {})
    saver.put_writes(_cfg(cp_id=c1["id"]), [("__interrupt__", "p")], task_id="t1")
    c2 = _ckpt("00000000-0000-0000-0000-000000000072", "ещё")
    saver.put(_cfg(cp_id=c1["id"]), c2, {}, {})

    saver.delete_thread("react:t1:hmacX")
    assert saver.get_tuple(_cfg()) is None  # весь тред снесён
    SF = saver._sf
    with SF() as s:
        n_cp = s.execute(text("select count(*) from react_checkpoint")).scalar()
        n_w = s.execute(text("select count(*) from react_checkpoint_write")).scalar()
    assert n_cp == 0 and n_w == 0


def test_reput_keeps_created_at(saver):
    """Идемпотентный re-put того же checkpoint_id НЕ меняет created_at (иначе GC by last-activity
    «омолодил» бы старый тред). created_at нет в set_ ON CONFLICT."""
    ck = _ckpt("00000000-0000-0000-0000-000000000090")
    saver.put(_cfg(), ck, {"step": 1}, {})
    SF = saver._sf
    with SF() as s:
        before = s.execute(text("select created_at from react_checkpoint")).scalar()
    saver.put(_cfg(), ck, {"step": 2}, {})  # re-put того же id (replay)
    with SF() as s:
        after = s.execute(text("select created_at from react_checkpoint")).scalar()
    assert before == after  # created_at стабилен на conflict


def test_put_writes_idempotent_replay(saver):
    """Повторный put_writes (replay/дубль) НЕ валит (атомарный ON CONFLICT) и дедупит idx≥0."""
    ck = _ckpt("00000000-0000-0000-0000-000000000080")
    saver.put(_cfg(), ck, {}, {})
    w = _cfg(cp_id=ck["id"])
    for _ in range(3):  # тройной replay — не должно падать на PK
        saver.put_writes(w, [("messages", "v"), ("__interrupt__", "p")], task_id="t1")
    SF = saver._sf
    with SF() as s:
        n = s.execute(text("select count(*) from react_checkpoint_write")).scalar()
    assert n == 2  # один обычный + один interrupt (без дублей)


# --- паритет с InMemorySaver ----------------------------------------------

def test_parity_with_inmemory(saver):
    mem = InMemorySaver()
    ck = _ckpt("00000000-0000-0000-0000-000000000060", "паритет")
    # InMemorySaver выносит channel_values в blob-таблицу ПО new_versions (мой saver инлайнит всё —
    # задизайненное различие). Для честного паритета задаём версии, чтобы InMemory тоже сохранил.
    ck["channel_versions"] = {"messages": "1"}
    new_versions = {"messages": "1"}
    meta = {"source": "loop", "step": 7}

    our_cfg = saver.put(_cfg(), ck, meta, new_versions)
    mem_cfg = mem.put(_cfg(), ck, meta, new_versions)
    assert our_cfg["configurable"]["checkpoint_id"] == mem_cfg["configurable"]["checkpoint_id"]

    saver.put_writes(_cfg(cp_id=ck["id"]), [("messages", "w")], task_id="t1")
    mem.put_writes(_cfg(cp_id=ck["id"]), [("messages", "w")], task_id="t1")

    og = saver.get_tuple(_cfg())
    mg = mem.get_tuple(_cfg())
    assert og.checkpoint["id"] == mg.checkpoint["id"]
    assert og.metadata == mg.metadata
    assert (og.checkpoint["channel_values"]["messages"][0].content
            == mg.checkpoint["channel_values"]["messages"][0].content)
    assert {ch for (_t, ch, _v) in og.pending_writes} == {ch for (_t, ch, _v) in mg.pending_writes}
