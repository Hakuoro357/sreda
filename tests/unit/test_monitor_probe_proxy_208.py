"""#208 монитор: внешние probe ходят через SOCKS-прокси КАК БОТ.

Groq НЕ в NO_PROXY → через HTTPS_PROXY (туннель); telegram/mimo/openrouter В NO_PROXY → direct.
+ max_severity='warning' понижает critical→warning (openrouter = депрекейченный прямой путь).
RED-before. importlib по пути (как test_monitor_failed_turns_140).
"""

import importlib.util
import sys
from pathlib import Path

_MONITOR = Path(__file__).resolve().parents[2] / "scripts" / "monitor_health.py"


def _load():
    spec = importlib.util.spec_from_file_location("monitor_health_208", _MONITOR)
    m = importlib.util.module_from_spec(spec)
    # dataclass-аннотации монитора (Literal Severity) резолвятся через sys.modules — регистрируем ДО exec.
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def test_proxy_for_url_groq_via_proxy_others_direct_208():
    mh = _load()
    mh._ENV = {
        "HTTPS_PROXY": "socks5://127.0.0.1:1080",
        "NO_PROXY": "localhost,127.0.0.1,token-plan-sgp.xiaomimimo.com,openrouter.ai,api.telegram.org",
    }
    # Groq НЕ в NO_PROXY → через прокси (зеркалит прод-STT)
    assert mh._proxy_for_url("https://api.groq.com/openai/v1/models") == "socks5://127.0.0.1:1080"
    # в NO_PROXY → direct (None)
    assert mh._proxy_for_url("https://openrouter.ai/api/v1/models") is None
    assert mh._proxy_for_url("https://api.telegram.org/botX/getMe") is None
    assert mh._proxy_for_url("https://token-plan-sgp.xiaomimimo.com/v1/models") is None


def test_proxy_for_url_none_without_proxy_208():
    mh = _load()
    mh._ENV = {}
    assert mh._proxy_for_url("https://api.groq.com/x") is None


def test_external_latency_max_severity_downgrade_208(monkeypatch):
    """max_severity='warning' понижает critical→warning (openrouter депрекейч)."""
    mh = _load()
    mh._ENV = {}

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise mh.httpx.ConnectError("Network is unreachable")

    monkeypatch.setattr(mh.httpx, "Client", _BoomClient)
    r1 = mh._external_latency("https://openrouter.ai/api/v1/models", "x")
    assert r1.status == "critical"  # дефолт
    r2 = mh._external_latency("https://openrouter.ai/api/v1/models", "x", max_severity="warning")
    assert r2.status == "warning"  # понижено


def test_external_latency_uses_proxy_for_groq_208(monkeypatch):
    """_external_latency для Groq передаёт proxy в httpx.Client (не direct)."""
    mh = _load()
    mh._ENV = {"HTTPS_PROXY": "socks5://127.0.0.1:1080", "NO_PROXY": "openrouter.ai"}
    captured = {}

    class _Resp:
        status_code = 200

    class _SpyClient:
        def __init__(self, *a, **k):
            captured.update(k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return _Resp()

    monkeypatch.setattr(mh.httpx, "Client", _SpyClient)
    mh._external_latency("https://api.groq.com/openai/v1/models", "groq_stt_latency")
    assert captured.get("proxy") == "socks5://127.0.0.1:1080", "Groq probe должен идти через прокси"
