"""#244 — fetch-движок fetch_via_filtered_egress + preflight_egress.

Через httpx.MockTransport (детерминированные ответы) + перехват kwargs httpx.Client (доказать
trust_env=False + proxy=PORT2 — acceptance #2). Покрытие: egress-конфиг; ручные редиректы с
ре-валидацией (private/non-http/https→http/too-many); сырой байт-лимит (Content-Length + поток);
gzip-бомба (decompressed-cap); br reject; pre-flight (proxy пуст/недоступен/ок).
"""
from __future__ import annotations

import gzip
import zlib

import httpx
import pytest

from sreda.services import fetch_url_client
from sreda.services.fetch_url_client import (
    MAX_RAW_FETCH_BYTES,
    BodyTooLarge,
    RedirectBlocked,
    UnsupportedEncoding,
    fetch_via_filtered_egress,
    preflight_egress,
)

_PROXY = "socks5://127.0.0.1:1081"


def _route(monkeypatch, handler) -> dict:
    """Подменить httpx.Client: перехватить kwargs + гнать ответы через MockTransport."""
    captured: dict = {}
    real_client = httpx.Client

    def factory(**kw):
        captured.update(kw)
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=kw.get("timeout"),
            headers=kw.get("headers"),
            follow_redirects=False,
        )

    monkeypatch.setattr(fetch_url_client.httpx, "Client", factory)
    return captured


def _resp(content=b"ok", *, status=200, ctype="text/plain", **extra_headers):
    headers = {"content-type": ctype, **extra_headers}
    # content как итератор → httpx делает СТРИМ-ответ (на eager-теле iter_raw в mock даёт StreamConsumed).
    return httpx.Response(status, headers=headers, content=iter([content]))


# ── egress-конфиг (acceptance #2) ────────────────────────────────────────
def test_client_uses_filtered_egress_not_env(monkeypatch):
    cap = _route(monkeypatch, lambda r: _resp(b"hello"))
    res = fetch_via_filtered_egress("https://example.com/", _PROXY)
    assert cap["trust_env"] is False           # НЕ читать HTTPS_PROXY=root-туннель
    assert cap["proxy"] == _PROXY              # только фильтр-egress PORT2
    assert cap["follow_redirects"] is False    # редиректы вручную
    assert res.text == "hello" and res.status == 200


# ── редиректы: следуем + ре-валидируем каждый хоп ────────────────────────
def test_redirect_followed_and_revalidated(monkeypatch):
    def h(r):
        if r.url.host == "start.com":
            return httpx.Response(302, headers={"location": "https://dest.com/p"})
        return _resp(b"final")

    _route(monkeypatch, h)
    res = fetch_via_filtered_egress("https://start.com/", _PROXY)
    assert "dest.com" in res.final_url and res.text == "final"


def test_redirect_to_private_blocked(monkeypatch):
    _route(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/"}))
    with pytest.raises(RedirectBlocked):
        fetch_via_filtered_egress("http://start.com/", _PROXY)  # http→http: ловит именно private-revalidate


def test_redirect_https_to_http_downgrade_blocked(monkeypatch):
    _route(monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://dest.com/"}))
    with pytest.raises(RedirectBlocked):
        fetch_via_filtered_egress("https://start.com/", _PROXY)


def test_redirect_to_nonhttp_blocked(monkeypatch):
    _route(monkeypatch, lambda r: httpx.Response(302, headers={"location": "ftp://dest.com/"}))
    with pytest.raises(RedirectBlocked):
        fetch_via_filtered_egress("http://start.com/", _PROXY)


def test_too_many_redirects(monkeypatch):
    _route(monkeypatch, lambda r: httpx.Response(302, headers={"location": "https://loop.com/next"}))
    with pytest.raises(RedirectBlocked):
        fetch_via_filtered_egress("https://loop.com/", _PROXY)


# ── байт-лимиты (анти-бомба) ─────────────────────────────────────────────
def test_content_length_too_large_rejected(monkeypatch):
    # заявленный Content-Length > лимита → reject ДО чтения тела
    _route(monkeypatch, lambda r: httpx.Response(
        200,
        headers={"content-type": "text/plain", "content-length": str(MAX_RAW_FETCH_BYTES + 1000)},
        content=iter([b"x"]),
    ))
    with pytest.raises(BodyTooLarge):
        fetch_via_filtered_egress("https://e.com/", _PROXY)


def test_raw_stream_too_large_aborts(monkeypatch):
    # без Content-Length: обрыв по СЫРОМУ счётчику iter_raw (ловит лживый/отсутствующий CL)
    big = b"x" * (MAX_RAW_FETCH_BYTES + 1000)
    _route(monkeypatch, lambda r: _resp(big))
    with pytest.raises(BodyTooLarge):
        fetch_via_filtered_egress("https://e.com/", _PROXY)


def test_gzip_bomb_decompressed_cap(monkeypatch):
    raw = gzip.compress(b"\0" * (fetch_url_client.MAX_DECOMPRESSED_BYTES + 100_000))
    assert len(raw) < MAX_RAW_FETCH_BYTES  # на проводе мал → проходит сырой лимит
    _route(monkeypatch, lambda r: _resp(raw, ctype="text/plain", **{"content-encoding": "gzip"}))
    with pytest.raises(BodyTooLarge):
        fetch_via_filtered_egress("https://e.com/", _PROXY)


def test_gzip_small_decoded_ok(monkeypatch):
    raw = gzip.compress("привет мир".encode())
    _route(monkeypatch, lambda r: _resp(
        raw, ctype="text/plain; charset=utf-8", **{"content-encoding": "gzip"}))
    res = fetch_via_filtered_egress("https://e.com/", _PROXY)
    assert "привет мир" in res.text


def test_brotli_encoding_rejected(monkeypatch):
    _route(monkeypatch, lambda r: _resp(b"whatever", **{"content-encoding": "br"}))
    with pytest.raises(UnsupportedEncoding):
        fetch_via_filtered_egress("https://e.com/", _PROXY)


# ── pre-flight ───────────────────────────────────────────────────────────
def test_preflight_empty_proxy():
    ok, reason = preflight_egress("")
    assert ok is False and "proxy" in reason


def test_preflight_proxy_unreachable(monkeypatch):
    def _raise(addr, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(fetch_url_client.socket, "create_connection", _raise)
    ok, reason = preflight_egress("socks5://127.0.0.1:65000")
    assert ok is False and "unreachable" in reason


def test_preflight_ok(monkeypatch):
    pytest.importorskip("socksio")  # httpx[socks] установлен в venv

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fetch_url_client.socket, "create_connection", lambda a, timeout: _Conn())
    ok, reason = preflight_egress(_PROXY)
    assert ok is True and reason == ""


# ── R1-фиксы: proxy ОБЯЗАН быть loopback-socks5 (анти-misconfig egress) ──
@pytest.mark.parametrize("proxy,sub", [
    ("http://127.0.0.1:8080", "scheme"),              # не socks → нельзя
    ("https://127.0.0.1:8080", "scheme"),
    ("socks5://1.2.3.4:1080", "loopback"),            # внешний host → нельзя
    ("socks5://evil.com:1080", "loopback"),
    ("socks5://user:pw@127.0.0.1:1081", "userinfo"),  # userinfo → нельзя
    ("socks5://127.0.0.1", "port"),                   # без порта → нельзя
])
def test_preflight_rejects_non_loopback_socks5_r1(proxy, sub):
    pytest.importorskip("socksio")
    ok, reason = preflight_egress(proxy)
    assert ok is False, proxy
    assert sub in reason.lower(), f"{proxy} → {reason}"


# ── R1-фиксы: raw-DEFLATE декодируется; усечённый gzip — reject ──
def test_raw_deflate_decoded_ok(monkeypatch):
    co = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)  # raw DEFLATE (без zlib-заголовка)
    raw = co.compress("deflate тело".encode()) + co.flush()
    _route(monkeypatch, lambda r: _resp(
        raw, ctype="text/plain; charset=utf-8", **{"content-encoding": "deflate"}))
    res = fetch_via_filtered_egress("https://e.com/", _PROXY)
    assert "deflate тело" in res.text


def test_truncated_gzip_rejected(monkeypatch):
    full = gzip.compress(bytes(range(256)) * 8)  # ~2KB варьированного → сжатие не до нуля
    truncated = full[: len(full) // 2]           # обрыв посреди deflate-данных
    _route(monkeypatch, lambda r: _resp(
        truncated, ctype="text/plain", **{"content-encoding": "gzip"}))
    with pytest.raises(UnsupportedEncoding):
        fetch_via_filtered_egress("https://e.com/", _PROXY)


# ── R2-фиксы: пустое тело + multi-member gzip ──
def test_empty_body_with_gzip_header_ok(monkeypatch):
    # сервер эхнул Content-Encoding: gzip на 0 байт → пусто, НЕ error (квота уже списана, no-refund)
    _route(monkeypatch, lambda r: _resp(b"", ctype="text/plain", **{"content-encoding": "gzip"}))
    res = fetch_via_filtered_egress("https://e.com/", _PROXY)
    assert res.text == ""


def test_multimember_gzip_decodes_all(monkeypatch):
    raw = gzip.compress(b"AAAA") + gzip.compress(b"BBBB")  # concat — оба члена валидны (RFC 1952)
    _route(monkeypatch, lambda r: _resp(raw, ctype="text/plain", **{"content-encoding": "gzip"}))
    res = fetch_via_filtered_egress("https://e.com/", _PROXY)
    assert res.text == "AAAABBBB"
