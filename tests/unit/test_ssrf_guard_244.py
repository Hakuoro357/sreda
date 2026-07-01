"""#244 — SSRF-гард fetch_url (best-effort early-reject; граница = nft на выходной ноде).

Покрытие (атакующая рамка из план-ревью R1-R5):
- блок приватных/служебных IP по ЕДИНОМУ denylist (is_global + явные CIDR; IPv4-mapped разворот; NAT64 64:ff9b::/96).
- numeric-obfuscated host (decimal-int / octal / hex-dotted) reject ДО DNS.
- схема http/https only; userinfo; нестандартный порт; IPv6 zone-id — reject.
- публичные адреса/домены — пропуск.
"""
from __future__ import annotations

import pytest

from sreda.services.ssrf_guard import (
    is_blocked_ip,
    looks_like_numeric_host,
    validate_fetch_url,
)


# ── is_blocked_ip: приватные/служебные → True, публичные → False ──
@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",  # loopback/RFC1918/metadata
    "100.64.0.1", "0.0.0.0", "240.0.0.1", "198.18.0.1", "192.0.0.1", "224.0.0.1",  # CGNAT/unspec/reserved/bench/multicast
    "::1", "fe80::1", "fc00::1", "ff02::1",  # v6 loopback/link-local/ULA/multicast
    "::ffff:127.0.0.1", "::ffff:169.254.169.254",  # IPv4-mapped
    "64:ff9b::7f00:1",  # NAT64 127.0.0.1 — is_global его НЕ ловит, явный CIDR обязан
    "2002:7f00:1::",  # 6to4 с вложенным приватным
])
def test_blocked_private_ips(ip):
    assert is_blocked_ip(ip) is True, ip


@pytest.mark.parametrize("ip", [
    "8.8.8.8", "1.1.1.1", "93.184.216.34",  # public IPv4
    "2606:4700:4700::1111",  # public IPv6 (cloudflare)
])
def test_allowed_public_ips(ip):
    assert is_blocked_ip(ip) is False, ip


def test_blocked_ip_garbage_fail_closed():
    # не-IP / мусор → True (fail-closed: не пропускаем неизвестное)
    assert is_blocked_ip("not-an-ip") is True
    assert is_blocked_ip("") is True


# ── numeric-obfuscated host ──
@pytest.mark.parametrize("host", [
    "2130706433",       # decimal int = 127.0.0.1
    "0x7f000001",       # hex int
    "0x7f.0.0.1",       # hex-dotted (утекало мимо isdigit, R2 субагент)
    "0177.0.0.1",       # octal-dotted
    "017700000001",     # octal int
])
def test_numeric_obfuscated_rejected(host):
    assert looks_like_numeric_host(host) is True, host


@pytest.mark.parametrize("host", [
    "example.com", "sub.example.co.uk", "wttr.in", "a1b2.example.com",
    "127.0.0.1",  # canonical dotted-quad — это валидный IP-литерал, ловится is_blocked_ip, НЕ numeric-эвристикой
])
def test_normal_host_not_numeric(host):
    assert looks_like_numeric_host(host) is False, host


# ── validate_fetch_url: полная валидация ДО сети ──
@pytest.mark.parametrize("url,reason_sub", [
    ("ftp://example.com/x", "scheme"),
    ("file:///etc/passwd", "scheme"),
    ("gopher://example.com", "scheme"),
    ("http://user:pass@example.com/", "userinfo"),
    ("http://example.com:8080/", "port"),
    ("http://2130706433/", "numeric"),
    ("http://0x7f.0.0.1/", "numeric"),
    ("http://[fe80::1%25eth0]/", "zone"),       # zone-id
    ("http://127.0.0.1/", "private"),            # литерал приватный
    ("http://169.254.169.254/latest/", "private"),
    ("", "empty"),
])
def test_validate_rejects(url, reason_sub):
    ok, reason = validate_fetch_url(url)
    assert ok is False, url
    assert reason_sub in reason.lower(), f"{url} → {reason}"


@pytest.mark.parametrize("url", [
    "https://example.com/path",
    "http://example.com:80/",
    "https://example.com:443/x?q=1",
    "https://wttr.in/Moscow?format=3",
    "https://8.8.8.8/",  # публичный IP-литерал — допустим
])
def test_validate_allows_public(url):
    ok, reason = validate_fetch_url(url)
    assert ok is True, f"{url} → {reason}"


# ── R1-ревью фиксы: short-form/trailing-dot numeric + localhost ──
@pytest.mark.parametrize("host", ["127.1", "127.0.1", "10.1", "1.2.3", "999.1.1.1", "0.1"])
def test_short_form_numeric_rejected_r1(host):
    # short-form/мусорный dotted-decimal: ipaddress не парсит → раньше проходил как «домен»
    assert looks_like_numeric_host(host) is True, host


@pytest.mark.parametrize("host", ["8.8.8.8", "127.0.0.1", "1.1.1.1"])
def test_canonical_ip_not_numeric_heuristic_r1(host):
    # канонический dotted-quad — НЕ numeric-эвристика (его ловит is_blocked_ip как настоящий IP)
    assert looks_like_numeric_host(host) is False, host


@pytest.mark.parametrize("url,sub", [
    ("http://127.1/", "numeric"),
    ("http://127.0.1/", "numeric"),
    ("http://999.1.1.1/", "numeric"),
    ("http://127.0.0.1./", "private"),       # trailing-dot FQDN-root → IP-литерал 127.0.0.1
    ("http://localhost/", "localhost"),
    ("http://localhost./", "localhost"),
    ("http://foo.localhost/", "localhost"),
])
def test_r1_bypasses_rejected(url, sub):
    ok, reason = validate_fetch_url(url)
    assert ok is False, url
    assert sub in reason.lower(), f"{url} → {reason}"
