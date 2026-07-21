# -*- coding: utf-8 -*-
"""#399: флаг+канарейка read-намерения и ЗАМЕР ОСТАТОЧНЫХ ПРОМАХОВ.

Флаг OFF → байт-в-байт текущее поведение (fail-open в web-only). Метрика остаточных
промахов — обязательная часть поставки: без неё фикс маскирует хвост ложных отказов.
"""
from __future__ import annotations



import pytest

from sreda.runtime.react_policy import compute_unified_policy, read_intent_residual_miss
from sreda.runtime.react_preflight import classify_checklist_query, route_domains
from sreda.runtime.react_signals import checklist_read_intent


def _fresh_settings(monkeypatch, **env: str):
    from sreda.config import settings as sm

    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sm.get_settings.cache_clear()
    return sm.get_settings()


class TestFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("SREDA_CHECKLIST_READ_INTENT", raising=False)
        assert _fresh_settings(monkeypatch).checklist_read_intent_enabled is False

    def test_env_alias(self, monkeypatch):
        s = _fresh_settings(monkeypatch, SREDA_CHECKLIST_READ_INTENT="1")
        assert s.checklist_read_intent_enabled is True

    def test_tenant_gate(self, monkeypatch):
        """Канарейка: пусто → никому; тенант → только он; ``*`` → все."""
        from sreda.config import settings as sm
        _fresh_settings(monkeypatch, SREDA_CHECKLIST_READ_INTENT="1")
        monkeypatch.delenv("SREDA_CHECKLIST_READ_INTENT_TENANTS", raising=False)
        sm.get_settings.cache_clear()
        assert "tenant_x" not in sm.get_settings().checklist_read_intent_tenants
        s = _fresh_settings(monkeypatch, SREDA_CHECKLIST_READ_INTENT_TENANTS="tenant_x")
        assert "tenant_x" in s.checklist_read_intent_tenants
        assert "tenant_y" not in s.checklist_read_intent_tenants
        s = _fresh_settings(monkeypatch, SREDA_CHECKLIST_READ_INTENT_TENANTS="*")
        assert "anyone" in s.checklist_read_intent_tenants

    def test_helper_requires_both_flag_and_tenant(self, monkeypatch):
        from sreda.runtime.react_loop import _checklist_read_intent_for
        _fresh_settings(monkeypatch, SREDA_CHECKLIST_READ_INTENT="1",
                        SREDA_CHECKLIST_READ_INTENT_TENANTS="t_owner")
        assert _checklist_read_intent_for("t_owner") is True
        assert _checklist_read_intent_for("t_other") is False
        _fresh_settings(monkeypatch, SREDA_CHECKLIST_READ_INTENT="0")
        assert _checklist_read_intent_for("t_owner") is False


class TestFlagOffIsIdentical:
    """OFF → политика И форма трейса прежние (fail-open в текущее поведение)."""

    @pytest.mark.parametrize("text", [
        "покажи список кино", "как дела", "покажи мои списки",
        "добавь в список кино матрицу", "какая погода",
    ])
    def test_policy_identical_when_source_not_supplied(self, text):
        route = route_domains(text)
        assert (compute_unified_policy(text, route)
                == compute_unified_policy(text, route, read_intent_domains=None))

    @pytest.mark.parametrize("text", ["покажи список кино", "как дела"])
    def test_signals_shape_unchanged_when_off(self, text):
        pol = compute_unified_policy(text, route_domains(text))
        assert "read_intent" not in pol["signals"]

    def test_flag_on_but_silent_signal_is_still_observable(self):
        """ON+сигнал молчит ≠ OFF: пустой frozenset обязан быть ВИДЕН в трейсе,
        иначе остаточные промахи не отличить от выключенной фичи."""
        text = "как дела"
        pol = compute_unified_policy(text, route_domains(text),
                                     read_intent_domains=frozenset())
        assert pol["signals"]["read_intent"] == []
        assert "checklists" not in pol["allowed_read"]


# ── замер остаточных промахов ──
# CR R1 sol MINOR: НЕ копируем формулу/регексы из продакшна (копия осталась бы зелёной
# при дрейфе проводки) — зовём ТУ ЖЕ функцию, что и react_loop.
def _residual_miss(text: str) -> bool:
    route = route_domains(text)
    pol = compute_unified_policy(text, route,
                                 read_intent_domains=checklist_read_intent(text))
    return read_intent_residual_miss(text, route, pol)


class TestResidualMissMetric:
    """Метрика ловит ИМЕННО остаточные промахи, не болтовню и не успехи."""

    @pytest.mark.parametrize("text", [
        "покажи список кино", "открой список подарков", "покажи мои списки",
        "какие у меня списки", "что в списке кино",
    ])
    def test_no_residual_when_read_opened(self, text):
        assert _residual_miss(text) is False

    @pytest.mark.parametrize("text", [
        "как дела", "как дела?", "привет, как дела", "как твои дела",
        "дела идут в гору",
    ])
    def test_smalltalk_is_not_counted_as_miss(self, text):
        """Мина «как дела» роутится в checklists и чтение НЕ открывает — но это
        НЕ промах, а корректный отказ. Без read-маркера метрика её не считает,
        иначе счётчик утонет в болтовне и станет бесполезным."""
        assert _residual_miss(text) is False

    @pytest.mark.parametrize("text", [
        # ИЗВЕСТНЫЙ ОСТАТОЧНЫЙ КЛАСС (замер 2026-07-21, ДО деплоя): вариант A его НЕ
        # закрывает — детектор даёт items/LOW без имени, и отделить его от мины «как дела»
        # внутри детектора нечем. Расширять сигнал языковыми паттернами ЗАПРЕЩЕНО
        # (директива владельца 2026-07-20) → это работа для варианта B (языко-нейтральный
        # read-бит), он вливается в тот же шов. Тест ФИКСИРУЕТ класс как известный:
        # покраснеет, когда B его закроет — тогда правим ожидание осознанно.
        "покажи список",            # без имени — детектор items/low
        "открой список",
        "открой дела",              # read-кюс требует «покажи/мои/наши/какие», «открой» не входит
        "покажи что там у меня по делам на завтра",  # детектор None
    ])
    def test_metric_fires_on_known_residual_misses(self, text):
        """Ровно тот хвост, ради которого метрика существует: юзер явно просит показать,
        роутер дал checklists, а чтение НЕ открылось → ложный отказ остаётся, но
        становится ВИДИМЫМ (`read_intent.residual_miss` в трейсе + строка в логе)."""
        assert "checklists" in route_domains(text).all_domains, "предпосылка класса"
        assert _residual_miss(text) is True, "промах обязан попасть в счётчик"

    def test_known_residual_is_not_silently_open(self):
        """Контроль: остаточный класс действительно НЕ открывает чтение (иначе тест
        выше зелёный по неверной причине)."""
        pol = compute_unified_policy(
            "покажи список", route_domains("покажи список"),
            read_intent_domains=checklist_read_intent("покажи список"))
        assert "checklists" not in pol["allowed_read"]
        assert classify_checklist_query("покажи список").confidence == "low"
