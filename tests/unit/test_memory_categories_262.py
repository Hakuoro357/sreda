"""#262 A1: схема категорий памяти — заземлённые контракты БД.

ВАЖНО (g-061): движок с `PRAGMA foreign_keys=ON` — дефолтный unit-SQLite держит FK OFF, и тест
composite-FK/каскада прошёл бы вхолостую (ловушка #74). Здесь FK реально энфорсятся.

Покрывает: факт привязывается к СВОЕЙ категории; факт НЕЛЬЗЯ привязать к категории чужого (tenant,user)
(composite FK, #53 DB-негатив); ровно одна system-категория (Common) на (tenant,user); уникальность имени
по name_normalized в скоупе; одинаковое имя у разных юзеров — ок.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.db.base import Base
from sreda.db.models.memory import AssistantMemory, MemoryCategory
from sreda.db.repositories.seed import SeedRepository
from sreda.services.text_normalization import normalize_for_dedup


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture()
def s():
    eng = create_engine("sqlite:///:memory:")

    @event.listens_for(eng, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    sess = Session(eng)
    seed = SeedRepository(sess)
    for sfx in ("a", "b"):  # два тенанта/юзера — FK ON требует реальных строк
        seed.ensure_tenant_bundle(
            tenant_id=f"tenant_{sfx}", tenant_name=sfx, workspace_id=f"ws_{sfx}", workspace_name=sfx,
            user_id=f"user_{sfx}", telegram_account_id=f"{sfx}1", assistant_id=f"as_{sfx}",
            assistant_name=sfx, eds_monitor_enabled=False)
    sess.commit()
    yield sess
    sess.close()
    eng.dispose()


def _cat(id_, tenant, user, name, *, is_system=False, slug=None):
    # T4: настоящая нормализация (а не lower().strip()) — иначе ложная уверенность в семантике name_normalized.
    return MemoryCategory(id=id_, tenant_id=tenant, user_id=user, name=name,
                          name_normalized=normalize_for_dedup(name), is_system=is_system, slug=slug,
                          created_at=_now())


def _mem(id_, tenant, user, category_id):
    return AssistantMemory(id=id_, tenant_id=tenant, user_id=user, tier="core", content="x",
                           source="agent_inferred", category_id=category_id, created_at=_now())


def test_fact_links_own_category_ok(s):
    s.add(_cat("cat_a", "tenant_a", "user_a", "Ткани"))
    s.commit()
    s.add(_mem("m1", "tenant_a", "user_a", "cat_a"))
    s.commit()  # своя категория → ок
    assert s.get(AssistantMemory, "m1").category_id == "cat_a"


def test_fact_cannot_link_other_user_category(s):
    """DB-негатив (#53): факт юзера B нельзя привязать к категории юзера A (composite FK)."""
    s.add(_cat("cat_a", "tenant_a", "user_a", "Ткани"))
    s.commit()
    s.add(_mem("m2", "tenant_b", "user_b", "cat_a"))  # (cat_a, tenant_b, user_b) нет в memory_categories
    with pytest.raises(IntegrityError):
        s.commit()


def test_one_system_category_per_user(s):
    s.add(_cat("c1", "tenant_a", "user_a", "Common", is_system=True, slug="common"))
    s.commit()
    s.add(_cat("c2", "tenant_a", "user_a", "Common2", is_system=True, slug="common"))
    with pytest.raises(IntegrityError):
        s.commit()


def test_name_normalized_unique(s):
    s.add(_cat("d1", "tenant_a", "user_a", "Работа"))
    s.commit()
    s.add(_cat("d2", "tenant_a", "user_a", "работа"))  # тот же name_normalized → дубль
    with pytest.raises(IntegrityError):
        s.commit()


def test_same_name_different_users_ok(s):
    s.add(_cat("e1", "tenant_a", "user_a", "Работа"))
    s.add(_cat("e2", "tenant_b", "user_b", "Работа"))  # другой скоуп → ок
    s.commit()
    assert s.get(MemoryCategory, "e2") is not None


# --- A3 ensure_common + резолв в save() + A2 backfill ---------------------------

def test_ensure_common_idempotent(s):
    from sreda.db.repositories.memory import MemoryRepository
    repo = MemoryRepository(s)
    cid1 = repo.ensure_common("tenant_a", "user_a")
    s.commit()
    cid2 = repo.ensure_common("tenant_a", "user_a")
    s.commit()
    assert cid1 == cid2  # та же Common
    n = (s.query(MemoryCategory)
         .filter_by(tenant_id="tenant_a", user_id="user_a", is_system=True).count())
    assert n == 1  # ровно одна


def test_save_without_category_goes_to_common(s):
    """Горячий путь: save() без category_id → факт в Common (не падает)."""
    from sreda.db.repositories.memory import MemoryRepository
    repo = MemoryRepository(s)
    m = repo.save("tenant_a", "user_a", tier="core", content="у меня дочь Маша")
    s.commit()
    assert m.category_id is not None
    cat = s.get(MemoryCategory, m.category_id)
    assert cat.is_system and cat.slug == "common"


def test_category_id_not_null_in_model(s):
    """M6: модель строит category_id NOT NULL → факт без категории отвергается (нет дрейфа модель↔прод, #74)."""
    bad = AssistantMemory(id="nn1", tenant_id="tenant_a", user_id="user_a", tier="core",
                          content="x", source="agent_inferred", created_at=_now())  # без category_id
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()


def test_user_cannot_create_duplicate_common_name(s):
    """C2: после авто-Common юзер НЕ создаст вторую «Общее» (name_normalized совпадает)."""
    from sreda.db.repositories.memory import CategoryNameConflict
    repo = _repo(s)
    repo.ensure_common("tenant_a", "user_a")
    s.commit()
    with pytest.raises(CategoryNameConflict):
        repo.create_category("tenant_a", "user_a", "Общее")


def test_reserved_common_name_rejected_before_common_exists(s):
    """R2 дыра порядка: «Общее» зарезервировано ДАЖЕ до создания Common — юзер не «украдёт» имя и не уронит
    последующий ensure_common/backfill на uq_memory_categories_name."""
    from sreda.db.repositories.memory import CategoryNameConflict
    repo = _repo(s)
    with pytest.raises(CategoryNameConflict):  # Common ещё НЕ создан, но имя уже зарезервировано
        repo.create_category("tenant_a", "user_a", "Общие")  # норм → «общий»
    s.rollback()
    # save после этого создаёт Common штатно (имя свободно)
    m = repo.save("tenant_a", "user_a", tier="core", content="факт")
    s.commit()
    assert s.get(MemoryCategory, m.category_id).is_system


def test_common_name_normalized_frozen_literal():
    """Сторож дрейфа: 0058 морозит литерал 'общий', repo считает живо — должны совпадать (иначе обновить 0058)."""
    from sreda.db.repositories.memory import COMMON_NAME_NORMALIZED
    assert normalize_for_dedup("Общее") == "общий"
    assert COMMON_NAME_NORMALIZED == "общий"


# --- A3 CRUD: категории + факты, скоуп и инварианты Common -----------------------

def _repo(s):
    from sreda.db.repositories.memory import MemoryRepository
    return MemoryRepository(s)


def test_create_category_and_duplicate_conflict(s):
    from sreda.db.repositories.memory import CategoryNameConflict
    repo = _repo(s)
    c = repo.create_category("tenant_a", "user_a", "Работа")
    s.commit()
    assert c.is_system is False and c.slug is None
    with pytest.raises(CategoryNameConflict):  # тот же name_normalized (лемма)
        repo.create_category("tenant_a", "user_a", "работа")


def test_rename_common_is_immutable(s):
    from sreda.db.repositories.memory import CategoryImmutable
    repo = _repo(s)
    cid = repo.ensure_common("tenant_a", "user_a")
    s.commit()
    with pytest.raises(CategoryImmutable):
        repo.rename_category("tenant_a", "user_a", cid, "Главная")


def test_rename_category_conflict_keeps_session_alive(s):
    """T1/C1: конфликт имени → CategoryNameConflict И сессия НЕ отравлена (иначе API отдал бы 500, не 409)."""
    from sreda.db.repositories.memory import CategoryNameConflict
    repo = _repo(s)
    repo.create_category("tenant_a", "user_a", "Работа")
    c2 = repo.create_category("tenant_a", "user_a", "Хобби")
    s.commit()
    c2_id = c2.id
    with pytest.raises(CategoryNameConflict):
        repo.rename_category("tenant_a", "user_a", c2_id, "работа")
    # C1: сессия жива — операции после конфликта НЕ падают PendingRollbackError
    assert s.query(MemoryCategory).filter_by(tenant_id="tenant_a", user_id="user_a").count() == 2
    assert s.get(MemoryCategory, c2_id).name == "Хобби"  # имя не повреждено (откат in-memory)
    s.commit()  # коммит не падает


def test_rename_category_not_found_returns_none(s):
    repo = _repo(s)
    assert repo.rename_category("tenant_a", "user_a", "nope", "X") is None


def test_delete_category_cascades_facts(s):
    repo = _repo(s)
    c = repo.create_category("tenant_a", "user_a", "Работа")
    s.commit()
    m1 = repo.save("tenant_a", "user_a", tier="core", content="факт 1", category_id=c.id)
    repo.save("tenant_a", "user_a", tier="core", content="факт 2", category_id=c.id)
    s.commit()
    m1_id, c_id = m1.id, c.id  # захватываем id строкой ДО удаления (иначе refresh истёкшего объекта)
    n = repo.delete_category("tenant_a", "user_a", c_id, confirm_count=2)
    s.commit()
    assert n == 2
    assert s.get(MemoryCategory, c_id) is None
    assert s.get(AssistantMemory, m1_id) is None  # факты удалены вместе с категорией


def test_delete_common_is_immutable(s):
    from sreda.db.repositories.memory import CategoryImmutable
    repo = _repo(s)
    cid = repo.ensure_common("tenant_a", "user_a")
    s.commit()
    with pytest.raises(CategoryImmutable):
        repo.delete_category("tenant_a", "user_a", cid, confirm_count=0)


def test_delete_category_confirm_mismatch(s):
    from sreda.db.repositories.memory import CategoryConfirmMismatch
    repo = _repo(s)
    c = repo.create_category("tenant_a", "user_a", "Работа")
    repo.save("tenant_a", "user_a", tier="core", content="факт", category_id=c.id)
    s.commit()
    with pytest.raises(CategoryConfirmMismatch):
        repo.delete_category("tenant_a", "user_a", c.id, confirm_count=0)  # клиент видел 0, по факту 1
    s.rollback()
    assert s.get(MemoryCategory, c.id) is not None  # ничего не удалили


def test_move_memory_to_other_users_category_is_404(s):
    repo = _repo(s)
    m = repo.save("tenant_a", "user_a", tier="core", content="мой факт")
    cb = repo.create_category("tenant_b", "user_b", "Чужая")
    s.commit()
    assert repo.move_memory("tenant_a", "user_a", m.id, cb.id) is None  # чужая цель → 404


def test_save_with_foreign_category_raises_not_found(s):
    """high m1: явный category_id чужого юзера → домен-404 (CategoryNotFound), а не IntegrityError/500."""
    from sreda.db.repositories.memory import CategoryNotFound
    repo = _repo(s)
    cb = repo.create_category("tenant_b", "user_b", "Чужая")
    s.commit()
    with pytest.raises(CategoryNotFound):
        repo.save("tenant_a", "user_a", tier="core", content="x", category_id=cb.id)


def test_edit_memory_reembeds_when_vector_provided(s):
    """T2/M5: при переданном новом векторе он СОХРАНЯЕТСЯ (факт остаётся находим recall'ом) + source=user_direct."""
    import json
    repo = _repo(s)
    m = repo.save("tenant_a", "user_a", tier="core", content="старое",
                  embedding=[0.1, 0.2], source="agent_inferred")
    s.commit()
    edited = repo.edit_memory("tenant_a", "user_a", m.id, content="новое", embedding=[0.3, 0.4, 0.5])
    s.commit()
    assert edited.content == "новое"
    assert edited.source == "user_direct"  # ручная правка
    assert json.loads(edited.embedding_json) == [0.3, 0.4, 0.5]  # контракт B: вектор пересчитан и сохранён
    assert edited.embedding_dim == 3


def test_edit_memory_without_vector_requires_explicit_flag(s):
    """M5/R2: без вектора И без явного флага → ValueError (защита от молчаливой потери recall); с флагом — ок."""
    repo = _repo(s)
    m = repo.save("tenant_a", "user_a", tier="core", content="старое", embedding=[0.1, 0.2])
    s.commit()
    m_id = m.id
    with pytest.raises(ValueError):
        repo.edit_memory("tenant_a", "user_a", m_id, content="новое")  # без embedding, без флага
    with pytest.raises(ValueError):
        repo.edit_memory("tenant_a", "user_a", m_id, content="новое", embedding=[])  # R3: пустой [] тоже гейт
    # явный деградированный режим — сбрасывает вектор осознанно
    edited = repo.edit_memory("tenant_a", "user_a", m_id, content="новое",
                              clear_embedding_for_manual_reembed=True)
    s.commit()
    assert edited.embedding_json is None and edited.embedding_dim == 0


def test_delete_memory_scoped(s):
    repo = _repo(s)
    m = repo.save("tenant_a", "user_a", tier="core", content="факт")
    s.commit()
    m_id = m.id  # захватываем строкой ДО удаления
    assert repo.delete_memory("tenant_b", "user_b", m_id) is False  # чужой → 404, не трогаем
    assert repo.delete_memory("tenant_a", "user_a", m_id) is True
    s.commit()
    assert s.get(AssistantMemory, m_id) is None


def test_list_categories_common_first(s):
    repo = _repo(s)
    repo.ensure_common("tenant_a", "user_a")
    repo.create_category("tenant_a", "user_a", "Работа")
    s.commit()
    cats = repo.list_categories("tenant_a", "user_a")
    assert cats[0].is_system is True  # Common первой
    assert {c.name for c in cats} >= {"Работа"}
