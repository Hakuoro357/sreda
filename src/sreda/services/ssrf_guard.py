"""#244 — SSRF-гард для fetch_url.

ВАЖНО (доказано план-ревью R1, по коду): авторитетная граница SSRF — nftables на ВЫХОДНОЙ ноде (httpcore SOCKS шлёт
ДОМЕН → резолвит выходная нода, app-резолв ≠ коннект). Этот модуль — **best-effort early-reject (A)**: режет очевидные
приватные/обфусцированные/не-http цели ДО сети (снижает шум, экономит egress, даёт быстрый отказ). НЕ авторитетная
граница для доменов — её даёт сетевой фильтр C.

`_DENY_*` — ЕДИНЫЙ источник denylist-диапазонов: из него же должен генерироваться nft-ruleset на выходной ноде
(sync-тест при реализации C). Список — IANA special-purpose + transition-префиксы (NAT64/6to4/Teredo/ULA/…), которые
`ipaddress.is_global` пропускает.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

# IPv4 special-purpose / private (CIDR). is_global ловит большинство; держим явно для паритета с nft (единый источник).
_DENY_V4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)
# IPv6 special-purpose + transition (is_global ПРОПУСКАЕТ NAT64/6to4/Teredo — обязателен явный список, R2/R5 ревью).
_DENY_V6 = (
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48",
    "100::/64", "2001::/32", "2001:2::/48", "2001:10::/28", "2001:20::/28",
    "2001:db8::/32", "2002::/16", "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10", "ff00::/8",
)
_DENY_NETS = tuple(ipaddress.ip_network(c) for c in (_DENY_V4 + _DENY_V6))

_HEX_LABEL_RE = re.compile(r"^0x[0-9a-f]+$", re.IGNORECASE)
_ALLOWED_PORTS = frozenset({80, 443})


def is_blocked_ip(ip_str: str) -> bool:
    """True, если IP приватный/служебный/transition (или мусор — fail-closed). False — публичный global."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # не IP / мусор → fail-closed (не пропускаем неизвестное)
    # IPv4-mapped IPv6 (::ffff:a.b.c.d) → проверяем вложенный IPv4 (is_global на mapped ведёт себя неоднозначно по версиям)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip in net for net in _DENY_NETS):  # явный denylist ловит NAT64/6to4/Teredo, что is_global пропускает
        return True
    return not ip.is_global  # широкий backstop для всего прочего не-глобального


def looks_like_numeric_host(host: str) -> bool:
    """True для numeric-ОБФУСЦИРОВАННОГО хоста (decimal-int / hex / octal-dotted), который НЕ канонический IP-литерал
    и который резолвер/прокси может истолковать как адрес. Канонический dotted-quad (127.0.0.1) → False (его ловит
    is_blocked_ip как настоящий IP). Закрывает обход «http://2130706433/», «http://0x7f.0.0.1/» (R2 субагент)."""
    h = (host or "").strip().rstrip(".")
    if not h:
        return False
    # host ТОЛЬКО из цифр и точек, но НЕ канонический IP-литерал → short-form (127.1/10.1/1.2.3) или
    # мусорный numeric (999.1.1.1) → обфускация (резолвер/прокси может трактовать как IPv4). Канонический
    # literal (127.0.0.1/8.8.8.8) парсится ipaddress → False (его ловит is_blocked_ip как настоящий IP).
    # R1 Codex high+medium MAJOR.
    if all(c.isdigit() or c == "." for c in h):
        try:
            ipaddress.ip_address(h)
            return False
        except ValueError:
            return True
    labels = h.split(".")
    if len(labels) == 1:  # single token: decimal-int / hex-int / octal-int
        lab = labels[0]
        return lab.isdigit() or bool(_HEX_LABEL_RE.match(lab))
    for lab in labels:
        if _HEX_LABEL_RE.match(lab):  # любой hex-label в dotted → обфускация (0x7f.0.0.1)
            return True
        if len(lab) > 1 and lab.startswith("0") and lab.isdigit():  # octal-label с ведущим нулём (0177.0.0.1)
            return True
    return False


def validate_components(
    *, scheme: str, host: str, port: int | None, has_userinfo: bool,
) -> tuple[bool, str]:
    """ЯДРО валидации по УЖЕ-распарсенным компонентам. (ok, reason).

    Вынесено отдельно, чтобы fetch-путь парсил URL РОВНО ОДИН раз (`httpx.URL`) и валидировал ИМЕННО тот объект,
    что пойдёт в запрос (анти parser-differential SSRF, CRITICAL R2): `urlsplit` тут и `httpx.URL` в запросе могли
    бы разойтись на хитром URL. Оба пути (`validate_fetch_url` для early-reject/тестов и fetch-клиент) зовут ЭТУ
    функцию — единственный источник политики."""
    if scheme not in ("http", "https"):
        return False, f"unsupported scheme: {scheme or '<none>'}"
    if has_userinfo:
        return False, "userinfo not allowed"
    host = host or ""
    if not host:
        return False, "missing host"
    if "%" in host:  # IPv6 zone-id (fe80::1%eth0) — reject целиком
        return False, "ipv6 zone-id not allowed"
    if port is not None and port not in _ALLOWED_PORTS:
        return False, f"port not allowed: {port}"
    # localhost-семейство (RFC 6761) — defense-in-depth early-reject. Та же функция валидирует КАЖДЫЙ
    # редирект-хоп, поэтому покрывает и редирект на localhost. R1 Codex high MAJOR / medium MINOR.
    host_norm = host.rstrip(".").lower()
    if host_norm == "localhost" or host_norm.endswith(".localhost"):
        return False, "localhost not allowed"
    if looks_like_numeric_host(host):
        return False, "numeric/obfuscated host not allowed"
    # IP-литерал? Обрезаем хвостовые точки FQDN-root (rstrip убирает ВСЕ): `127.0.0.1.`/`127.0.0.1..`
    # резолвятся в `127.0.0.1` → должны ловиться is_blocked_ip, а не проходить как «домен». R1 субагент MAJOR.
    host_ip = host.rstrip(".")
    try:
        ipaddress.ip_address(host_ip)  # host — IP-литерал?
    except ValueError:
        return True, ""  # домен: app-резолв best-effort (граница C); пропускаем дальше
    if is_blocked_ip(host_ip):
        return False, "private/blocked ip literal"
    return True, ""


def validate_fetch_url(raw: str) -> tuple[bool, str]:
    """Удобная обёртка ДО сети по строке (early-reject + тесты). Делегирует в `validate_components`."""
    u = (raw or "").strip()
    if not u:
        return False, "empty url"
    try:
        parts = urlsplit(u)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return False, "invalid host/port"
    except Exception:  # noqa: BLE001
        return False, "invalid url"
    return validate_components(
        scheme=parts.scheme, host=host, port=port,
        has_userinfo=bool(parts.username or parts.password),
    )
