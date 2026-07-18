"""#115 live smoke — реальные запросы через ПРОДОВЫЙ агентский цикл локально.

Гоняет реальные русские фразы через conversation.chat (тот же handler, что
обслуживает прод-Telegram): настоящий MiMo LLM + настоящие housewife-инструменты
на локальной SQLite. Проверяемое поведение #115: инструменты возвращают okv2 с
ИМЕНАМИ, голос агента называет предметы по именам и различает группы
(добавила / уже было / не нашла / уже куплено).

НЕ трогает прод. НЕ CI-тест (живые LLM-вызовы стоят денег) — ручной прогон:

    .venv/Scripts/python.exe scripts/dev/live_115_smoke.py \
        [--input block.json] [--out transcript.md] [--db live115.db]

--input: JSON-список [{"req": "...", "old": "...", "turn": "..."}] — реальные
фразы (например, выгрузка ходов боевого тенанта по технологии #87; сам
tenant-id сюда не коммитим — audit 2026-07-18).
Без --input гоняется встроенный синтетический набор из 12 фраз.
PII-транскрипты класть в sreda/plans/_*.md (gitignored), не в parent plans/.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

_ARGS = argparse.ArgumentParser()
_ARGS.add_argument("--input", default=None)
_ARGS.add_argument("--out", default="C:/pro/vex-assistant/plans/write-tool-detailed-outputs-live-smoke-r1.md")
_ARGS.add_argument("--db", default="live115_smoke.db")
CLI = _ARGS.parse_args()

# --- env ДО импорта sreda (settings кэшируются) -----------------------------
_REPO = Path(__file__).resolve().parents[2]
_DB_PATH = Path(tempfile.gettempdir()) / CLI.db
if _DB_PATH.exists():
    _DB_PATH.unlink()
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef"
).decode("ascii")
os.environ["SREDA_FEATURE_MODULES"] = "sreda_feature_housewife_assistant"
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ.setdefault("SREDA_ENV", "dev")
os.environ.setdefault("SREDA_TG_ACCOUNT_SALT", "live115_local_smoke_salt_not_prod")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from datetime import datetime, timedelta, timezone  # noqa: E402
from uuid import uuid4  # noqa: E402

from sreda.config.settings import get_settings  # noqa: E402
from sreda.db.base import Base  # noqa: E402
from sreda.db.models import Assistant, Tenant, User, Workspace  # noqa: E402
from sreda.db.models.billing import SubscriptionPlan, TenantSubscription  # noqa: E402
from sreda.db.repositories.memory import MemoryRepository  # noqa: E402
from sreda.db.session import get_engine, get_session_factory  # noqa: E402
from sreda.runtime.executor import ActionRuntimeService  # noqa: E402
from sreda.runtime.dispatcher import ActionEnvelope  # noqa: E402
from sreda.services import housewife_chat_tools as hct  # noqa: E402
from sreda.services.llm import get_chat_llm  # noqa: E402

TRANSCRIPT = Path(CLI.out)

PHRASES = [
    # — покупки: created / dup-existing / dup-in-batch / bought / not_eligible /
    #   removed / rename —
    "добавь в покупки молоко, хлеб и сыр",
    "добавь ещё хлеб и кефир",
    "добавь молоко, творог и ещё раз молоко",
    "отметь что я купила молоко и хлеб",
    "я купила молоко",
    "убери сыр из списка покупок",
    "исправь в покупках кефир на ряженку",
    # — задачи —
    "добавь задачу позвонить сантехнику завтра",
    "отметь задачу про сантехника выполненной",
    # — рецепт / семья / чек-лист —
    "сохрани рецепт борща: свёкла, капуста, говядина, картошка",
    "добавь в семью дочь Машу, ей 9 лет",
    "создай чек-лист Дача и добавь туда грабли и перчатки",
]


class ConstantEmbeddingClient:
    dim = 8

    def embed_document(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"ok": True}

    async def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"ok": True}

    async def delete_message(self, *, chat_id, message_id):
        return {"ok": True}


# --- шпион на инструменты: пишем (имя, аргументы, сырой okv2-вывод) ----------
TOOL_LOG: list[tuple[str, str, str]] = []
_orig_build = hct.build_housewife_tools


def _spy_build(**kwargs):
    tools = _orig_build(**kwargs)
    for t in tools:
        fn = getattr(t, "func", None)
        if fn is None:
            continue

        def _make(name, inner):
            def spy(*a, **kw):
                try:
                    out = inner(*a, **kw)
                except Exception as exc:  # noqa: BLE001
                    TOOL_LOG.append((name, repr(kw or a), f"RAISED {exc!r}"))
                    raise
                TOOL_LOG.append((name, repr(kw or a), str(out)))
                return out

            return spy

        try:
            t.func = _make(t.name, fn)
        except Exception:  # noqa: BLE001
            object.__setattr__(t, "func", _make(t.name, fn))
    return tools


hct.build_housewife_tools = _spy_build


def seed(session) -> None:
    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.flush()
    session.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="Sreda"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"housewife_assistant_basic_{uuid4().hex[:8]}",
        feature_key="housewife_assistant",
        title="Housewife Basic",
        description="",
        price_rub=0,
        credits_monthly_quota=1_000_000_000,
    )
    session.add(plan)
    session.flush()
    session.add(
        TenantSubscription(
            id=f"sub_{uuid4().hex[:16]}",
            tenant_id="t1",
            plan_id=plan.id,
            feature_key="housewife_assistant",
            status="active",
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            active_until=datetime.now(timezone.utc) + timedelta(days=30),
        )
    )
    session.commit()
    # одна core-память — гасит онбординг (skip-эвристика handler'а)
    repo = MemoryRepository(session)
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="Обращайся ко мне на ты. Семья: я и дочь.",
        embedding=[1.0] + [0.0] * 7,
        source="user_direct",
    )
    session.commit()


def envelope(text: str) -> ActionEnvelope:
    return ActionEnvelope(
        action_type="conversation.chat",
        tenant_id="t1",
        workspace_id="w1",
        assistant_id="a1",
        user_id="u1",
        channel_type="telegram_dm",
        external_chat_id="42",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value=text,
        params={"text": text},
    )


def main() -> int:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    seed(session)

    llm = get_chat_llm(provider="mimo-v2.5-pro")
    if llm is None:
        print("FATAL: get_chat_llm вернул None — нет ключа?")
        return 2

    telegram = FakeTelegram()
    svc = ActionRuntimeService(
        session,
        telegram_client=telegram,
        llm_client=llm,
        embedding_client=ConstantEmbeddingClient(),
    )

    if CLI.input:
        records = json.loads(Path(CLI.input).read_text(encoding="utf-8"))
    else:
        records = [{"req": p, "old": None, "turn": None} for p in PHRASES]

    lines: list[str] = [
        "# #115 live smoke — реальные фразы через прод-агентский цикл",
        "",
        f"Дата: {datetime.now().isoformat(timespec='seconds')}. "
        f"Вход: {CLI.input or 'встроенный синтетический набор'}. "
        "LLM: mimo-v2.5-pro (живой). Инструменты: настоящие, SQLite локально. "
        "Прод не тронут.",
        "",
    ]

    def flush() -> None:
        TRANSCRIPT.write_text("\n".join(lines), encoding="utf-8")

    for i, rec in enumerate(records, 1):
        phrase = rec["req"]
        tool_mark = len(TOOL_LOG)
        sent_mark = len(telegram.sent)
        edit_mark = len(telegram.edited)
        print(f"[{i}/{len(records)}] {phrase[:90]}", flush=True)
        suffix = f" (архивный ход {rec['turn']})" if rec.get("turn") else ""
        lines.append(f"## Ход {i}{suffix}: «{phrase}»")
        if rec.get("old"):
            lines.append("")
            lines.append(f"**Старый ответ (прод, архив):** {rec['old']}")
            lines.append("")
        try:
            queued = svc.enqueue_action(envelope(phrase))
            asyncio.run(svc.process_job(queued.job_id))
        except Exception:  # noqa: BLE001
            lines.append("**ОШИБКА ХОДА:**")
            lines.append("```")
            lines.append(traceback.format_exc()[-2000:])
            lines.append("```")
            flush()
            continue

        for name, args, out in TOOL_LOG[tool_mark:]:
            lines.append(f"- 🔧 `{name}` args={args}")
            lines.append(f"  - провод: `{out[:500]}`")
        replies = [d["text"] for d in telegram.sent[sent_mark:]]
        edits = [d["text"] for d in telegram.edited[edit_mark:]]
        final = (replies or edits or ["(нет ответа)"])[-1]
        lines.append("")
        lines.append(f"**Голос:** {final}")
        lines.append("")
        flush()
        print(f"    -> {final[:120]}", flush=True)

    flush()
    print(f"\nТранскрипт: {TRANSCRIPT}", flush=True)
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
