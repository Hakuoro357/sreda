from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_connect_rate_limit
from sreda.config.settings import Settings, get_settings
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient
from sreda.services.eds_account_verification import EDSAccountVerificationService
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService

router = APIRouter(tags=["connect"])
logger = logging.getLogger(__name__)


def _enforce_same_origin(request: Request, settings: Settings) -> None:
    """Reject cross-origin POSTs to the connect form.

    Real browsers always send ``Origin`` on POST requests since the
    Fetch spec landed in every evergreen engine, so an Origin that
    does not match the public base URL means the request originated
    from a different site (classic CSRF). A missing ``Origin`` is
    accepted for server-side clients and tests — browsers never omit
    it on same-origin POSTs that our rendered form triggers, so the
    relaxation does not widen the browser attack surface.
    """

    origin = request.headers.get("origin")
    if origin is None:
        return
    expected_base = (settings.connect_public_base_url or "").strip().rstrip("/")
    if not expected_base:
        return
    try:
        expected = urlsplit(expected_base)
        actual = urlsplit(origin.rstrip("/"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin_invalid") from exc
    if (
        expected.scheme != actual.scheme
        or expected.hostname != actual.hostname
        or (expected.port or None) != (actual.port or None)
    ):
        logger.warning(
            "connect form POST rejected: cross-origin submission (origin=%s)",
            origin,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin_mismatch")


@router.get(
    "/connect/eds/{token}",
    response_class=HTMLResponse,
    dependencies=[Depends(enforce_connect_rate_limit)],
)
def open_eds_connect_form(
    token: str,
    session: Session = Depends(get_db_session),
) -> HTMLResponse:
    service = EDSConnectService(session, get_settings())
    try:
        connect_session = service.open_form(token)
    except ConnectSessionError as exc:
        return HTMLResponse(_render_error_page(exc.message), status_code=exc.status_code)
    return HTMLResponse(
        _render_form_page(
            account_slot_type=connect_session.account_slot_type,
            expires_at=_format_expires_at(connect_session.expires_at),
        ),
        status_code=200,
    )


@router.post(
    "/connect/eds/{token}",
    response_class=HTMLResponse,
    dependencies=[Depends(enforce_connect_rate_limit)],
)
async def submit_eds_connect_form(
    token: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> HTMLResponse:
    settings = get_settings()
    _enforce_same_origin(request, settings)
    service = EDSConnectService(session, settings)
    body = await request.body()
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return HTMLResponse(_render_error_page("Некорректные данные формы."), status_code=400)
    login = (parsed.get("login") or [""])[0]
    password = (parsed.get("password") or [""])[0]
    try:
        result = service.submit_form(token, login=login, password=password)
    except ConnectSessionError as exc:
        if exc.code == "session_used":
            return HTMLResponse(_render_submitted_page(already_started=True), status_code=200)
        return HTMLResponse(_render_error_page(exc.message), status_code=exc.status_code)
    telegram_client = TelegramClient(settings.telegram_bot_token) if settings.telegram_bot_token else None
    verifier = EDSAccountVerificationService(session, telegram_client=telegram_client)
    inline_result: str | None = None
    try:
        inline_result = await verifier.process_job(result.job_id)
    except Exception:
        logger.exception("Inline EDS verification kick failed for job %s", result.job_id)
    # Показываем "подключено" только если верификация реально прошла. Любой
    # другой исход (failed / retry_scheduled / claimed_by_other / exception)
    # — нейтральный текст "результат придёт в Telegram": именно туда уходит
    # итоговое сообщение (success или failure).
    return HTMLResponse(
        _render_submitted_page(verified=(inline_result == "completed")),
        status_code=200,
    )


def _render_form_page(*, account_slot_type: str, expires_at: str) -> str:
    # Load Telegram.WebApp SDK + wire BackButton → /miniapp/. Without
    # this, the arrow in Telegram's header on this page does nothing:
    # the form opens inside the Mini App WebView and the user's only
    # escape was closing the whole WebView (×). Now the back arrow
    # cleanly returns to the subscriptions Mini App.
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Подключение EDS</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
      * {{
        box-sizing: border-box;
      }}
      body {{
        margin: 0;
        padding: 16px;
        font-family: Arial, sans-serif;
        line-height: 1.5;
        color: #111827;
        background: #ffffff;
      }}
      main {{
        max-width: 560px;
        margin: 0 auto;
      }}
      h1 {{
        margin: 0 0 24px;
        font-size: 32px;
        line-height: 1.15;
      }}
      p {{
        margin: 0 0 16px;
      }}
      .expires-at {{
        color: #4b5563;
      }}
      label {{
        display: block;
        margin-bottom: 16px;
      }}
      .label-text {{
        display: block;
        margin-bottom: 8px;
        font-size: 16px;
      }}
      input {{
        display: block;
        width: 100%;
        max-width: 100%;
        padding: 12px;
        font-size: 16px;
        border: 1px solid #d1d5db;
        border-radius: 10px;
        background: #ffffff;
      }}
      button {{
        padding: 12px 18px;
        font-size: 16px;
        border: 0;
        border-radius: 10px;
        background: #2563eb;
        color: #ffffff;
        cursor: pointer;
      }}
      button[disabled] {{
        background: #93c5fd;
        cursor: not-allowed;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>Подключение кабинета EDS</h1>
      <p>Это защищенная одноразовая страница для подключения личного кабинета EDS.</p>
      <p>Логин и пароль передаются по защищенному соединению и сохраняются в системе только в зашифрованном виде.</p>
      <p>Введите логин и пароль и нажмите кнопку "Подключить"</p>
      <p class="expires-at">Ссылка действует до: {escape(expires_at)}</p>
      <form method="post">
        <label>
          <span class="label-text">Логин</span>
          <input type="text" name="login" autocomplete="username">
        </label>
        <label>
          <span class="label-text">Пароль</span>
          <input type="password" name="password" autocomplete="current-password">
        </label>
        <button type="submit" id="submit-button">Подключить</button>
      </form>
    </main>
    <script>
      const form = document.querySelector("form");
      const submitButton = document.getElementById("submit-button");
      if (form && submitButton) {{
        form.addEventListener("submit", () => {{
          submitButton.disabled = true;
          submitButton.textContent = "Проверяем...";
        }}, {{ once: true }});
      }}
      (function() {{
        var tg = window.Telegram && window.Telegram.WebApp;
        if (!tg) return;
        tg.ready();
        tg.expand();
        if (tg.BackButton) {{
          tg.BackButton.show();
          tg.BackButton.onClick(function() {{
            window.location.href = "/miniapp/";
          }});
        }}
      }})();
    </script>
  </body>
</html>"""


def _render_submitted_page(*, already_started: bool = False, verified: bool = False) -> str:
    # Title + heading должны соответствовать message. Если оставлять
    # "Данные получены" для любого исхода — пользователь читает это как
    # подтверждение успеха, даже когда inline-kick фактически упал.
    if already_started:
        title = "Проверка уже идёт"
        message = "Проверка уже запущена. Статус будет виден в приложении."
    elif verified:
        title = "Кабинет EDS подключён"
        message = "Мониторинг активен."
    else:
        # Нейтральная формулировка для всех не-успешных исходов (failed /
        # retry_scheduled / claimed_by_other / exception): форму мы
        # приняли, но про успех или провал узнаем через Telegram.
        title = "Форма отправлена"
        message = "Результат проверки появится в приложении."
    # Страница запускается в Telegram WebView (миниапп-контекст): подключаем
    # Telegram.WebApp SDK, показываем системную BackButton и даём крупную
    # кнопку возврата в Mini App /subscriptions. Без SDK BackButton не
    # реагирует на onClick — отсюда и был "назад не работает".
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
        background: var(--tg-theme-bg-color, #fff);
        color: var(--tg-theme-text-color, #000);
        line-height: 1.5;
        margin: 0 auto;
        padding: 16px;
        max-width: 480px;
      }}
      h1 {{ font-size: 22px; margin: 16px 0 12px; }}
      p {{ margin: 8px 0; color: var(--tg-theme-hint-color, #666); }}
      .btn {{
        display: block;
        width: 100%;
        padding: 12px 20px;
        margin-top: 24px;
        border: none;
        border-radius: 8px;
        font-size: 15px;
        font-weight: 600;
        text-align: center;
        text-decoration: none;
        cursor: pointer;
        background: var(--tg-theme-button-color, #007aff);
        color: var(--tg-theme-button-text-color, #fff);
        -webkit-tap-highlight-color: transparent;
      }}
      .btn:active {{ opacity: 0.7; }}
    </style>
  </head>
  <body>
    <h1>{escape(title)}</h1>
    <p>{escape(message)}</p>
    <a href="/miniapp/" class="btn" id="back-btn">Вернуться в подписки</a>
    <script>
      (function() {{
        var tg = window.Telegram && window.Telegram.WebApp;
        if (!tg) return;
        tg.ready();
        tg.expand();
        var goBack = function() {{ window.location.href = "/miniapp/"; }};
        if (tg.BackButton) {{
          tg.BackButton.show();
          tg.BackButton.onClick(goBack);
        }}
      }})();
    </script>
  </body>
</html>"""


def _render_error_page(message: str) -> str:
    # Dead-link handling: the most common reason a user lands here is
    # an old Telegram message with a stale web_app button (TTL 15 min
    # + admin resets can nuke the session). We must NOT leave them
    # stranded — add the same WebApp SDK + "Вернуться в подписки"
    # button pattern as the success page so one tap gets them back
    # into the Mini App where a fresh link is one tap away.
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Ошибка подключения</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
        background: var(--tg-theme-bg-color, #fff);
        color: var(--tg-theme-text-color, #000);
        line-height: 1.5;
        margin: 0 auto;
        padding: 16px;
        max-width: 480px;
      }}
      h1 {{ font-size: 22px; margin: 16px 0 12px; }}
      p {{ margin: 8px 0; color: var(--tg-theme-hint-color, #666); }}
      .btn {{
        display: block;
        width: 100%;
        padding: 12px 20px;
        margin-top: 24px;
        border: none;
        border-radius: 8px;
        font-size: 15px;
        font-weight: 600;
        text-align: center;
        text-decoration: none;
        cursor: pointer;
        background: var(--tg-theme-button-color, #007aff);
        color: var(--tg-theme-button-text-color, #fff);
        -webkit-tap-highlight-color: transparent;
      }}
      .btn:active {{ opacity: 0.7; }}
    </style>
  </head>
  <body>
    <h1>Ошибка подключения</h1>
    <p>{escape(message)}</p>
    <p>Откройте подписки и нажмите «Подключить ЛК EDS» — ссылка создастся заново.</p>
    <a href="/miniapp/" class="btn">Вернуться в подписки</a>
    <script>
      (function() {{
        var tg = window.Telegram && window.Telegram.WebApp;
        if (!tg) return;
        tg.ready();
        tg.expand();
        var goBack = function() {{ window.location.href = "/miniapp/"; }};
        if (tg.BackButton) {{
          tg.BackButton.show();
          tg.BackButton.onClick(goBack);
        }}
      }})();
    </script>
  </body>
</html>"""


def _format_expires_at(value: datetime) -> str:
    try:
        localized = value.astimezone()
    except ValueError:
        localized = value
    return localized.strftime("%d.%m.%Y %H:%M")
