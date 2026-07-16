"""#376: subtract-only дизамбигуация неоднозначной read-кюс-группы.

На «Что у меня в списке кино» щедрый read_cue кладёт в allowed_read {checklists, shopping}
(слово «список» → группа), и mercury может позвать list_shopping (промах +покупки, прод
2026-07-15). Умный классификатор (disambiguator) стабильно говорит «checklists» → его
high-вердикт ВЫЧИТАЕТ остальных членов неоднозначной группы из allowed_read.

Инварианты (owner + ревью-трио R1/R2):
- ТОЛЬКО вычитание (add невозможен by construction): Фредди-домен вне поднятых → не применяем.
- write не трогаем (allowed_write исключён из subtract).
- compound/cross пропускаем (reminders/menu целы).
- disambiguator=None → байт-в-байт текущее поведение (#285/#352).

RED до реализации: параметр disambiguator в compute_unified_policy ещё не существует.
"""
from __future__ import annotations

from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import DomainClassResult, route_domains


def _pol(text, disambiguator=None):
    return compute_unified_policy(text, route_domains(text), disambiguator=disambiguator)


def test_disambig_subtracts_shopping_376():
    """«список кино» + Фредди=checklists/high → shopping ВЫЧТЕН из allowed_read."""
    pol = _pol("Что у меня в списке кино", DomainClassResult(("checklists",), "high"))
    assert "checklists" in pol["allowed_read"]
    assert "shopping" not in pol["allowed_read"]  # член неоднозначной группы вычтен


def test_disambig_off_byte_identical_376():
    """disambiguator=None → shopping остаётся (текущее поведение не тронуто)."""
    pol = _pol("Что у меня в списке кино", None)
    assert "shopping" in pol["allowed_read"]


def test_disambig_low_untouched_376():
    """Фредди low → политику НЕ трогаем (fail-safe)."""
    pol = _pol("Что у меня в списке кино", DomainClassResult((), "low"))
    assert "shopping" in pol["allowed_read"]


def test_disambig_add_not_applied_376():
    """Фредди-домен ВНЕ поднятых (add-режим, возможная инъекция) → НЕ применяем.
    «покажи задачи» поднимает tasks; Фредди говорит shopping (не поднято) → allowed_read не трогаем."""
    text = "покажи задачи"
    pol = _pol(text, DomainClassResult(("shopping",), "high"))
    # shopping НЕ добавлен (add запрещён — только вычитание из поднятых)
    assert "shopping" not in pol["allowed_read"]


def test_disambig_compound_untouched_376():
    """Составной «кино и напомни хлеб» (compound) → дизамбигуация ПРОПУЩЕНА целиком:
    allowed_read с дизамбигуатором == без него (мой код не вмешивается в compound)."""
    text = "покажи список кино и напомни купить хлеб"
    r = route_domains(text)
    assert r.compound_by_connector, "тест-предпосылка: это compound"
    base = compute_unified_policy(text, r)
    dis = compute_unified_policy(text, r, disambiguator=DomainClassResult(("checklists",), "high"))
    assert base["allowed_read"] == dis["allowed_read"], "compound: дизамбигуация не должна менять read"
    assert dis["signals"]["disambig_kind"] is None, "compound: дизамбигуация должна быть пропущена"


def test_disambig_cross_untouched_376():
    """Cross «покупки из меню» → дизамбигуация пропущена, menu+shopping целы."""
    text = "составь покупки из меню"
    r = route_domains(text)
    pol = compute_unified_policy(text, r, disambiguator=DomainClassResult(("shopping",), "high"))
    if r.cross_intent:  # направленное кросс-намерение
        assert "menu" in pol["allowed_read"] and "shopping" in pol["allowed_read"]


def test_disambig_does_not_touch_write_376():
    """subtract НЕ снимает домен, который является write-целью (allowed_read ⊇ allowed_write инвариант)."""
    pol = _pol("Что у меня в списке кино", DomainClassResult(("checklists",), "high"))
    for wd in pol["allowed_write"]:
        assert wd in pol["allowed_read"], "write-цель осталась читаемой"


# ─────────────────── проводка: флаг-гейт, дедуп нотификаций, fail-safe ───────────────────


def _flag376(monkeypatch, enabled: bool, tenants: str | None):
    import sreda.config.settings as sm
    monkeypatch.setenv("SREDA_DOMAIN_CLF_DISAMBIG", "1" if enabled else "0")
    if tenants is None:
        monkeypatch.delenv("SREDA_DOMAIN_CLF_DISAMBIG_TENANTS", raising=False)
    else:
        monkeypatch.setenv("SREDA_DOMAIN_CLF_DISAMBIG_TENANTS", tenants)
    sm.get_settings.cache_clear()


def test_gate_off_by_default_376(monkeypatch):
    """Дефолт: флаг OFF → никому (байт-в-байт текущее поведение)."""
    from sreda.runtime.react_loop import _domain_clf_disambig_for
    _flag376(monkeypatch, False, None)
    assert _domain_clf_disambig_for("tenant_max_40921122") is False


def test_gate_on_allowlist_376(monkeypatch):
    """ON + тенант в списке → True; не в списке → False; `*` → все."""
    from sreda.runtime.react_loop import _domain_clf_disambig_for
    _flag376(monkeypatch, True, "tenant_max_40921122")
    assert _domain_clf_disambig_for("tenant_max_40921122") is True
    assert _domain_clf_disambig_for("tenant_tg_777") is False
    _flag376(monkeypatch, True, "*")
    assert _domain_clf_disambig_for("tenant_tg_777") is True


def test_gate_tenants_without_flag_376(monkeypatch):
    """Список без флага → OFF (обе ручки обязательны, как #285)."""
    from sreda.runtime.react_loop import _domain_clf_disambig_for
    _flag376(monkeypatch, False, "*")
    assert _domain_clf_disambig_for("tenant_max_40921122") is False


def test_notify_dedup_same_key_once_376(monkeypatch):
    """Одинаковое расхождение (тот же кортеж) в TTL-окне → ОДИН алерт; другое → проходит."""
    import asyncio as _a
    from sreda.runtime import react_loop as rl
    sent: list = []

    async def main():
        rl._DIS376_SEEN.clear()
        real_create = _a.create_task

        def _capture(coro, **kw):
            sent.append(coro)
            return real_create(_noop())
        monkeypatch.setattr(rl.asyncio, "create_task", _capture)
        dis1 = {"static_domains": ["checklists", "shopping"], "freddie_domains": ["checklists"],
                "kind": "subtract", "applied": True}
        rl._notify_domain_divergence("tenant_x", dis1)
        rl._notify_domain_divergence("tenant_x", dis1)          # дубль → не шлём
        dis2 = {**dis1, "kind": "add", "applied": False}         # другой вид → шлём
        rl._notify_domain_divergence("tenant_x", dis2)

    async def _noop():
        return None

    _a.run(main())
    for c in sent:  # закрыть неисполненные корутины (анти-warning)
        c.close()
    assert len(sent) == 2, f"ожидали 2 алерта (уникальные ключи), получили {len(sent)}"


def test_notify_never_raises_376(monkeypatch):
    """Сбой доставки/окружения НИКОГДА не роняет ход (fire-and-forget, глотаем)."""
    from sreda.runtime import react_loop as rl
    rl._DIS376_SEEN.clear()
    # вне event loop create_task кинет RuntimeError → общий except должен проглотить
    rl._notify_domain_divergence("tenant_x", {"static_domains": ["a"], "freddie_domains": ["b"],
                                              "kind": "add", "applied": False})  # не должно raise


def test_send_alert_swallows_376(monkeypatch):
    """_dis376_send_alert глотает исключение доставки."""
    import asyncio as _a
    from sreda.runtime import react_loop as rl

    async def _boom(text):
        raise RuntimeError("down")
    import sreda.services.admin_alerts as aa
    monkeypatch.setattr(aa, "alert_admin_async", _boom)
    _a.run(rl._dis376_send_alert("test"))  # не должно raise
