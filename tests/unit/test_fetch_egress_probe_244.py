"""#244 монитор: инверсная проба probe_fetch_egress_filtered (3 шага).

(1) proxy=loopback-socks5 иначе critical-misconfig; (2) TCP-preflight туннеля — мёртв→warning;
(3) GET 169.254.169.254 через ЖИВОЙ proxy: ЛЮБОЙ ответ (200/403/…)→critical, исключение→ok.
Идёт через SREDA_FETCH_URL_PROXY (фильтр), не root :1080; trust_env=False. importlib по пути (как #208).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "monitor_health.py"


def _load():
    spec = importlib.util.spec_from_file_location("monitor_health_244", _MONITOR)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


class _Cm:  # context-manager заглушка
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sock_ok(monkeypatch, mh):
    monkeypatch.setattr(mh.socket, "create_connection", lambda *a, **k: _Cm())


def _sock_down(monkeypatch, mh):
    def _raise(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(mh.socket, "create_connection", _raise)


def _httpx_resp(monkeypatch, mh, status, captured=None):
    class _Resp:
        status_code = status

    class _C(_Cm):
        def __init__(self, *a, **k):
            if captured is not None:
                captured["kwargs"] = k

        def get(self, url):
            if captured is not None:
                captured["url"] = url
            return _Resp()

    monkeypatch.setattr(mh.httpx, "Client", _C)


def _httpx_raise(monkeypatch, mh):
    class _C(_Cm):
        def __init__(self, *a, **k):
            pass

        def get(self, url):
            raise mh.httpx.ConnectError("refused")

    monkeypatch.setattr(mh.httpx, "Client", _C)


_PROXY = "socks5://127.0.0.1:1081"


def test_blocked_via_live_proxy_is_ok_244(monkeypatch):
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY}
    _sock_ok(monkeypatch, mh)
    _httpx_raise(monkeypatch, mh)
    assert mh.probe_fetch_egress_filtered().status == "ok"


def test_socks_blocked_is_ok_244(monkeypatch):
    # РЕАЛЬНЫЙ blocked-сигнал через SOCKS — socksio.ProtocolError «Malformed reply» (leak мимо httpx,
    # НЕ TransportError) — доказано live на проде 2026-06-30. Должен классифицироваться как ok, не warning.
    socksio_exc = pytest.importorskip("socksio.exceptions")
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY}
    _sock_ok(monkeypatch, mh)

    class _C(_Cm):
        def __init__(self, *a, **k):
            pass

        def get(self, url):
            raise socksio_exc.ProtocolError("Malformed reply")

    monkeypatch.setattr(mh.httpx, "Client", _C)
    assert mh.probe_fetch_egress_filtered().status == "ok"


def test_metadata_200_is_critical_244(monkeypatch):
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY}
    _sock_ok(monkeypatch, mh)
    _httpx_resp(monkeypatch, mh, 200)
    assert mh.probe_fetch_egress_filtered().status == "critical"


@pytest.mark.parametrize("status", [301, 403, 404, 500, 502])
def test_metadata_any_response_is_critical_244(monkeypatch, status):
    # анти gate-на-200: ЛЮБОЙ ответ = link-local достигнут = фильтр пробит (R1 adjudication)
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY}
    _sock_ok(monkeypatch, mh)
    _httpx_resp(monkeypatch, mh, status)
    assert mh.probe_fetch_egress_filtered().status == "critical", f"{status}-ответ = пробой"


def test_tunnel_down_is_warning_not_ok_244(monkeypatch):
    # туннель мёртв → fetch fail-closed, но граница НЕ проверена → warning (не ok). R1 MAJOR-2
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY}
    _sock_down(monkeypatch, mh)
    r = mh.probe_fetch_egress_filtered()
    assert r.status == "warning" and "недоступен" in r.message


@pytest.mark.parametrize("proxy", [
    "http://127.0.0.1:1081",            # не socks
    "socks5://1.2.3.4:1081",            # не loopback
    "socks5://127.0.0.1",               # без порта
    "socks5://user:pw@127.0.0.1:1081",  # userinfo (прод-preflight отвергает)
    "socks5://@127.0.0.1:1081",         # пустой userinfo-marker (presence, не truthiness) — R3
    "socks5://:@127.0.0.1:1081",        # пустой user:pass marker — R3
    "socks5://127.0.0.1:notaport",      # нечисловой порт → ValueError
    "socks5://127.0.0.1:99999",         # порт out-of-range → ValueError
])
def test_misconfigured_proxy_is_critical_244(monkeypatch, proxy):
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": proxy}
    r = mh.probe_fetch_egress_filtered()
    assert r.status == "critical" and "границу" in r.message


def test_trailing_dot_localhost_accepted_244(monkeypatch):
    # localhost. (FQDN-root) = валидный loopback (как прод-preflight rstrip) → НЕ misconfig, идёт дальше. R3
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": "socks5://localhost.:1081"}
    _sock_ok(monkeypatch, mh)
    _httpx_raise(monkeypatch, mh)
    assert mh.probe_fetch_egress_filtered().status == "ok"


def test_setup_error_is_warning_not_ok_244(monkeypatch):
    # непредвиденная ошибка в шаге 3 (НЕ transport) → граница не проверена → warning, НЕ ложное ok
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY}
    _sock_ok(monkeypatch, mh)

    class _C(_Cm):
        def __init__(self, *a, **k):
            pass

        def get(self, url):
            raise RuntimeError("unexpected setup failure")

    monkeypatch.setattr(mh.httpx, "Client", _C)
    r = mh.probe_fetch_egress_filtered()
    assert r.status == "warning" and "не проверена" in r.message


def test_uses_fetch_proxy_trust_env_false_and_metadata_url_244(monkeypatch):
    mh = _load()
    mh._ENV = {"SREDA_FETCH_URL_PROXY": _PROXY, "HTTPS_PROXY": "socks5://127.0.0.1:1080"}
    _sock_ok(monkeypatch, mh)
    cap = {}
    _httpx_resp(monkeypatch, mh, 200, captured=cap)
    mh.probe_fetch_egress_filtered()
    assert cap["kwargs"].get("proxy") == _PROXY, "через фильтр 1081, не root 1080"
    assert cap["kwargs"].get("trust_env") is False
    assert cap["kwargs"].get("follow_redirects") is False, "3xx не следовать (=ответ→critical)"
    assert cap["url"].startswith("http://169.254.169.254"), "цель = metadata-IP"


def test_skip_when_proxy_unset_244():
    mh = _load()
    mh._ENV = {}
    r = mh.probe_fetch_egress_filtered()
    assert r.status == "ok" and "skip" in r.message.lower()


def test_registered_in_probes_244():
    mh = _load()
    assert mh.probe_fetch_egress_filtered in mh.PROBES
