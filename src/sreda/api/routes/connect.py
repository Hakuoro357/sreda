from __future__ import annotations

import logging
from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sreda.api.deps import enforce_connect_rate_limit
from sreda.db.session import get_db_session

router = APIRouter(tags=["connect"])
logger = logging.getLogger(__name__)

# #181: eds_monitor retired. The connect routes stay mounted (old Telegram
# messages / Mini App buttons link here) but are pure tombstones — they render
# a "disabled" page (HTTP 200, not 404) and perform NO DB mutation. Phase 2:
# the live EDSConnectService / verification flow was removed with its module,
# so the route bodies are now nothing but the disabled page.
_EDS_DISABLED_PAGE_MESSAGE = "Это умение отключено."


@router.get(
    "/connect/eds/{token}",
    response_class=HTMLResponse,
    dependencies=[Depends(enforce_connect_rate_limit)],
)
def open_eds_connect_form(
    token: str,
    session: Session = Depends(get_db_session),
) -> HTMLResponse:
    return HTMLResponse(_render_error_page(_EDS_DISABLED_PAGE_MESSAGE), status_code=200)


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
    return HTMLResponse(_render_error_page(_EDS_DISABLED_PAGE_MESSAGE), status_code=200)


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
