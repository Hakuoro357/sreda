# -*- coding: utf-8 -*-
"""#214 — миграция MAX API на platform-api2 + корни Минцифры.

Гарды: корень Минцифры в репо = опубликованный (анти-подмена); SSLContext
MAX-клиента доверяет ему; базовый URL берётся из настройки (дефолт
platform-api2, откат через env).
"""
from __future__ import annotations

import hashlib
import ssl

import pytest

import sreda.integrations.max.client as mc

# SHA-256 корней Минцифры (сверены при заведении #214: Root — с gu-st.ru и
# по цепочке platform-api2; Sub — из AIA листа nuc-cdp.voskhod.ru, SKI 77:3D:D9…
# = AKI листа *.max.ru). Пин обоих — анти-подмена ЛЮБОГО загружаемого в verify-
# контекст корня (Codex R1 MAJOR: Sub тоже trust-anchor для MAX-клиента).
_ROOT_SHA256 = (
    "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"
)
_SUB_SHA256 = (
    "2155785036c900dbb5f1bb2a1569c80c55595bd6bf94867a29bbddbc7d88a3f2"
)


def _sha256_of_pem(path) -> str:
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def test_root_ca_present_and_fingerprint_pinned():
    """Корень в репо = опубликованный Russian Trusted Root CA (анти-подмена)."""
    p = mc._CERTS_DIR / "russian_trusted_root_ca.pem"
    assert p.exists(), f"нет файла корня: {p}"
    assert _sha256_of_pem(p) == _ROOT_SHA256, "отпечаток корня НЕ совпал — подмена?"


def test_sub_ca_present_and_fingerprint_pinned():
    """Промежуточный Sub (subca_ssl_rsa2024) = тот, что подписал leaf MAX —
    запинен по SHA-256 (подмена Sub-файла = расширение доверия MAX-клиента)."""
    p = mc._CERTS_DIR / "russian_trusted_sub_ca_ssl_rsa2024.pem"
    assert p.exists(), f"нет файла Sub: {p}"
    assert _sha256_of_pem(p) == _SUB_SHA256, "отпечаток Sub НЕ совпал — подмена?"


def test_ru_ca_files_is_exactly_the_two_pinned():
    """Список загружаемых в verify-контекст файлов зафиксирован — нельзя тихо
    добавить третий непроверенный корень."""
    assert mc._RU_CA_FILES == (
        "russian_trusted_root_ca.pem",
        "russian_trusted_sub_ca_ssl_rsa2024.pem",
    )


def test_max_ssl_context_trusts_russian_root():
    """SSLContext MAX-клиента доверяет корню Минцифры (+ стандартным CA)."""
    mc.max_ssl_context.cache_clear()
    ctx = mc.max_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    subjects = str(ctx.get_ca_certs())
    assert "Russian Trusted Root CA" in subjects, "корень Минцифры не загружен в контекст"


def test_max_ssl_context_fail_fast_if_cert_missing(monkeypatch, tmp_path):
    """Codex R2: отсутствие обязательного корня → RuntimeError (fail-fast), а не
    тихий контекст без Минцифры с отложенным verify-fail."""
    mc.max_ssl_context.cache_clear()
    monkeypatch.setattr(mc, "_CERTS_DIR", tmp_path)  # пустой каталог — сертов нет
    try:
        with pytest.raises(RuntimeError):
            mc.max_ssl_context()
    finally:
        mc.max_ssl_context.cache_clear()


def test_max_base_url_default_is_platform_api2(monkeypatch):
    """Дефолт базового URL — новый адрес platform-api2."""
    from sreda.config import settings as st

    monkeypatch.delenv("SREDA_MAX_API_BASE_URL", raising=False)
    st.get_settings.cache_clear()
    try:
        assert mc.max_base_url() == "https://platform-api2.max.ru"
    finally:
        st.get_settings.cache_clear()


def test_max_base_url_env_override_rollback(monkeypatch):
    """Откат на старый адрес — сменой env, без редеплоя."""
    from sreda.config import settings as st

    monkeypatch.setenv("SREDA_MAX_API_BASE_URL", "https://platform-api.max.ru")
    st.get_settings.cache_clear()
    try:
        assert mc.max_base_url() == "https://platform-api.max.ru"
    finally:
        st.get_settings.cache_clear()


@pytest.mark.parametrize(
    "bad",
    [
        "http://platform-api2.max.ru",                 # не https → токен открытым текстом
        "https://evil.example.com",                    # чужой host → утечка токена
        "https://user:pass@platform-api2.max.ru",      # userinfo
        "ftp://platform-api2.max.ru",                  # не https
        "https://max.ru",                              # bare — не в allowlist (Codex R2)
        "https://foo.max.ru",                          # чужой поддомен max.ru
        "https://platform-api2.max.ru:4443",           # нестандартный порт
        "https://platform-api2.max.ru/path",           # path
        "https://platform-api2.max.ru?x=1",            # query
    ],
)
def test_max_api_base_url_rejects_insecure_or_foreign(monkeypatch, bad):
    """Codex R1 MAJOR: плохой env отвергается на загрузке конфига (fail-fast),
    чтобы токен MAX не ушёл в открытом виде / на чужой хост."""
    from sreda.config import settings as st

    monkeypatch.setenv("SREDA_MAX_API_BASE_URL", bad)
    st.get_settings.cache_clear()
    try:
        with pytest.raises(Exception):
            st.get_settings()
    finally:
        st.get_settings.cache_clear()
