from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient
from sreda.services.eds_account_verification import EDSAccountVerificationService
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService

router = APIRouter(tags=["connect"])
logger = logging.getLogger(__name__)


@router.get("/connect/eds/{token}", response_class=HTMLResponse)
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


@router.post("/connect/eds/{token}", response_class=HTMLResponse)
async def submit_eds_connect_form(
    token: str,
    request: Request,
    session: Session = Depends(get_db_session),
) -> HTMLResponse:
    settings = get_settings()
    service = EDSConnectService(session, settings)
    body = await request.body()
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    login = (parsed.get("login") or [""])[0]
    password = (parsed.get("password") or [""])[0]
    try:
        result = service.submit_form(token, login=login, password=password)
    except ConnectSessionError as exc:
        return HTMLResponse(_render_error_page(exc.message), status_code=exc.status_code)
    telegram_client = TelegramClient(settings.telegram_bot_token) if settings.telegram_bot_token else None
    verifier = EDSAccountVerificationService(session, telegram_client=telegram_client)
    try:
        await verifier.process_job(result.job_id)
    except Exception:
        logger.exception("Inline EDS verification kick failed for job %s", result.job_id)
    return HTMLResponse(_render_submitted_page(), status_code=200)


def _render_form_page(*, account_slot_type: str, expires_at: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Подключение EDS</title>
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
        <button type="submit">Подключить</button>
      </form>
    </main>
  </body>
</html>"""


def _render_submitted_page() -> str:
    return """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Данные получены</title>
  </head>
  <body>
    <main style="max-width:560px;margin:40px auto;font-family:Arial,sans-serif;line-height:1.5;">
      <h1>Данные получены</h1>
      <p>Сейчас проверяем доступ к кабинету EDS.</p>
      <p>Можно закрыть эту страницу и вернуться в Telegram.</p>
    </main>
  </body>
</html>"""


def _render_error_page(message: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Ошибка подключения</title>
  </head>
  <body>
    <main style="max-width:560px;margin:40px auto;font-family:Arial,sans-serif;line-height:1.5;">
      <h1>Ошибка подключения</h1>
      <p>{escape(message)}</p>
    </main>
  </body>
</html>"""


def _format_expires_at(value: datetime) -> str:
    try:
        localized = value.astimezone()
    except ValueError:
        localized = value
    return localized.strftime("%d.%m.%Y %H:%M")
