"""#244 — fetch-движок для fetch_url через ВЫДЕЛЕННЫЙ фильтр-egress (defense-in-depth A).

Граница SSRF — nft на выходной ноде (C); здесь — app-слой:
1. `preflight_egress` — ДО reserve квоты (no-refund): socksio есть И proxy задан И локальный PORT2 жив (TCP-probe).
2. `fetch_via_filtered_egress` — `httpx.Client(trust_env=False, proxy=PORT2)` (НЕ общий root-туннель), ручные
   редиректы с ре-валидацией каждого хопа (анти rebinding/parser-differential), `iter_raw` СЫРОЙ байт-лимит +
   bounded decompress (анти gzip-бомба).

trust_env=False — КРИТИЧНО: иначе httpx прочитает HTTPS_PROXY=root-туннель из окружения → C обойдён.
"""
from __future__ import annotations

import logging
import socket
import zlib
from dataclasses import dataclass

import httpx

from sreda.services.ssrf_guard import validate_components

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 15.0
MAX_REDIRECTS = 5
MAX_RAW_FETCH_BYTES = 5_000_000      # сырой потолок на чтение (анти gzip-бомба «на проводе»)
MAX_DECOMPRESSED_BYTES = 8_000_000   # потолок ПОСЛЕ decompress (gzip-бомба раздувается из малого raw)
PREFLIGHT_PROBE_TIMEOUT = 2.0
FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Просим ТОЛЬКО gzip → нужен лишь zlib; deflate-ambiguity (RFC1950 vs 1951) и br не просим. Если сервер
# всё же пришлёт deflate — _decompress_bounded пробует zlib- и raw-вариант. R1 субагент MINOR.
_ACCEPT_ENCODING = "gzip"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# fetch-egress proxy ОБЯЗАН быть локальным фильтр-туннелем socks5 (анти-misconfig → root/внешний proxy).
_ALLOWED_PROXY_SCHEMES = frozenset({"socks5", "socks5h"})
_LOOPBACK_PROXY_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class FetchEgressError(Exception):
    """База ошибок fetch-движка (caller мапит в error:<...>)."""


class RedirectBlocked(FetchEgressError):
    """Редирект/URL отклонён ре-валидацией (private/не-http(s)/https→http/userinfo/numeric)."""


class BodyTooLarge(FetchEgressError):
    """Тело превысило сырой или decompressed байт-лимит (анти-бомба)."""


class UnsupportedEncoding(FetchEgressError):
    """Сервер прислал content-encoding, который мы не декодируем безопасно (напр. br)."""


@dataclass(slots=True)
class FetchResult:
    status: int
    final_url: str
    content_type: str
    text: str


def preflight_egress(proxy_url: str | None) -> tuple[bool, str]:
    """Pre-flight ДО reserve квоты (no-refund). (ok, reason).

    Все три обязательны: (1) `socksio` импортируется, (2) `proxy_url` задан и парсится, (3) локальный PORT2
    отвечает на TCP-connect. Любой не ОК → fail-closed (caller: `error:fetch_egress_unavailable`), reserve НЕ делаем.
    """
    if not proxy_url:
        return False, "proxy not configured"
    try:
        import socksio  # noqa: F401
    except ImportError:
        return False, "socksio missing"
    try:
        pu = httpx.URL(proxy_url)
        scheme, host, port = pu.scheme, pu.host, pu.port
    except Exception:  # noqa: BLE001
        return False, "proxy url invalid"
    # ЖЁСТКО: только локальный фильтр-egress socks5. trust_env=False спасает от env HTTPS_PROXY, но НЕ от
    # подменённого/ошибочного SREDA_FETCH_URL_PROXY (внешний proxy / root-порт / не-socks) — без этой
    # проверки egress-инвариант обходится конфигом. R1 Codex high+medium MAJOR.
    if scheme not in _ALLOWED_PROXY_SCHEMES:
        return False, "proxy scheme not allowed"
    if (host or "").rstrip(".").lower() not in _LOOPBACK_PROXY_HOSTS:
        return False, "proxy host not loopback"
    if pu.username or pu.password:
        return False, "proxy userinfo not allowed"
    if not port:
        return False, "proxy port required"
    try:
        with socket.create_connection((host, port), timeout=PREFLIGHT_PROBE_TIMEOUT):
            pass
    except OSError:
        return False, "proxy unreachable"
    return True, ""


def _decompress_bounded(raw: bytes, encoding: str, cap: int) -> bytes:
    """Decompress с потолком на ВЫХОД (анти gzip-бомба). identity → как есть; gzip/deflate → zlib bounded;
    иное (br/…) → UnsupportedEncoding (Accept-Encoding мы не просили br, но сервер мог проигнорить)."""
    enc = (encoding or "").lower().strip()
    if not raw:                            # пустое тело → пусто (для любого encoding); сервер мог эхнуть
        return b""                         # Content-Encoding на 0 байт. R2 субагент MINOR
    if enc in ("", "identity"):
        return raw[:cap]
    if enc == "gzip":
        wbits = 16 + zlib.MAX_WBITS
    elif enc == "deflate":
        wbits = zlib.MAX_WBITS             # zlib-wrapped (RFC 1950); raw-DEFLATE — фолбэк ниже
    else:
        raise UnsupportedEncoding(enc)

    def _one_member(data: bytes, wb: int, budget: int) -> tuple[bytes, bytes]:
        """Декод ОДНОГО zlib/gzip-члена с потолком budget на выход. → (out, unused_data)."""
        d = zlib.decompressobj(wb)
        out = d.decompress(data, budget)   # max_length=budget → не раздуть память бомбой
        if d.unconsumed_tail:              # вход остался → член вышел за budget → бомба
            raise BodyTooLarge()
        out += d.flush()
        if not d.eof:                      # член не завершён (усечён/битый) → НЕ принимаем частично. R1 Codex high MINOR
            raise UnsupportedEncoding(f"{enc}: incomplete stream")
        return out, d.unused_data

    result = b""
    data = raw
    while True:
        budget = cap - len(result)
        if budget <= 0:                    # ещё члены, но cap исчерпан → бомба
            raise BodyTooLarge()
        try:
            out, rest = _one_member(data, wbits, budget)
        except zlib.error as exc:
            if enc == "deflate" and not result:   # raw-DEFLATE (RFC 1951, без zlib-заголовка) — повтор. R1 субагент MINOR
                try:
                    out, rest = _one_member(data, -zlib.MAX_WBITS, budget)
                except zlib.error as exc2:
                    raise UnsupportedEncoding(f"deflate: {exc2}") from exc2
            else:
                raise UnsupportedEncoding(f"{enc}: {exc}") from exc
        result += out
        if not rest:                       # больше членов нет
            break
        data = rest                        # multi-member concat (RFC 1952) — декодим следующий. R2 medium+субагент MINOR
    return result


def _decode_text(body: bytes, content_type: str) -> str:
    """bytes → str по charset из content-type, иначе utf-8 (errors=replace)."""
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return body.decode("utf-8", errors="replace")


def fetch_via_filtered_egress(raw_url: str, proxy_url: str) -> FetchResult:
    """GET `raw_url` через фильтр-egress PORT2. Ручные редиректы (ре-валидация хопов), iter_raw байт-лимит,
    bounded decompress. Бросает FetchEgressError/httpx-исключения — caller ловит (квота уже списана, no-refund)."""
    with httpx.Client(
        trust_env=False,            # НЕ читать HTTPS_PROXY/env — иначе root-туннель, C обойдён
        proxy=proxy_url,            # httpx 0.28: единственное число
        follow_redirects=False,     # редиректы вручную — ре-валидируем каждый хоп
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": FETCH_UA, "Accept-Encoding": _ACCEPT_ENCODING},
    ) as client:
        current = httpx.URL(raw_url)
        for _hop in range(MAX_REDIRECTS + 1):
            # валидируем ИМЕННО объект, что пойдёт в запрос (один парсер — анти parser-differential)
            ok, reason = validate_components(
                scheme=current.scheme, host=current.host, port=current.port,
                has_userinfo=bool(current.username or current.password),
            )
            if not ok:
                raise RedirectBlocked(reason)
            with client.stream("GET", current) as resp:
                if resp.status_code in _REDIRECT_STATUSES and "location" in resp.headers:
                    loc = (resp.headers.get("location") or "").strip()
                    if not loc:
                        raise RedirectBlocked("redirect without location")
                    nxt = current.join(loc)
                    if current.scheme == "https" and nxt.scheme == "http":
                        raise RedirectBlocked("https->http downgrade")
                    current = nxt
                    continue
                # финальный ответ. Ранний reject по заявленному Content-Length.
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > MAX_RAW_FETCH_BYTES:
                    raise BodyTooLarge()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_raw():
                    total += len(chunk)
                    if total > MAX_RAW_FETCH_BYTES:   # обрыв по СЫРОМУ лимиту (ловит лживый CL)
                        raise BodyTooLarge()
                    chunks.append(chunk)
                raw_body = b"".join(chunks)
                body = _decompress_bounded(
                    raw_body, resp.headers.get("content-encoding", ""), MAX_DECOMPRESSED_BYTES,
                )
                ctype = resp.headers.get("content-type", "").lower()
                return FetchResult(
                    status=resp.status_code,
                    final_url=str(resp.url),
                    content_type=ctype,
                    text=_decode_text(body, ctype),
                )
        raise RedirectBlocked("too many redirects")
