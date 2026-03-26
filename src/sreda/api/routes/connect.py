from __future__ import annotations

import logging
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
            expires_at=connect_session.expires_at.isoformat(),
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
    slot_label = "первого кабинета" if account_slot_type == "primary" else "дополнительного кабинета"
    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Подключение EDS</title>
  </head>
  <body>
    <main style="max-width:560px;margin:40px auto;font-family:Arial,sans-serif;line-height:1.5;">
      <h1>Подключение кабинета EDS</h1>
      <p>Форма для {escape(slot_label)}. Данные будут сохранены в зашифрованном виде.</p>
      <p><small>Ссылка действует ограниченное время: {escape(expires_at)}</small></p>
      <form method="post">
        <label style="display:block;margin-bottom:16px;">
          <span>Логин</span><br>
          <input type="text" name="login" autocomplete="username" style="width:100%;padding:10px;">
        </label>
        <label style="display:block;margin-bottom:16px;">
          <span>Пароль</span><br>
          <input type="password" name="password" autocomplete="current-password" style="width:100%;padding:10px;">
        </label>
        <button type="submit" style="padding:10px 16px;">Подключить</button>
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
