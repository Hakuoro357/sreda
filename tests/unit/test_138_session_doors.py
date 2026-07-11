"""#138 Ф5 (#353, срез-8): AST-сторож «дверей» к БД — механическая полнота покрытия.

Проблема, которую закрывает: 5 раундов ревью Ф5-5b/5c находили неском-скоупленные
обращения к RLS-таблицам ПО ОДНОМУ (read-path → inbound-хвост → channel-link →
queue). Ревью — дисциплина; owner-bar #138 требует МЕХАНИЗМ. Этот тест механически
перечисляет ВСЕ точки создания «сырых» сессий/фабрик в src/sreda (двери мимо швов
``tenant_session``/``privileged_session``) и сверяет с замороженным allow-list, где
КАЖДАЯ дверь классифицирована: почему она безопасна после флипа DSN.

Новая дверь без классификации → красный: либо используй шов, либо классифицируй
здесь с причиной (как rls_registry для таблиц). Ключ — (файл, функция, примитив),
БЕЗ номеров строк (дрейф строк не триггерит).

Это drift-гейт, не доказательство корректности записей: сами классификации
проверены Ф2-картой (plans/138-f2-remainder-map.md) + 5 раундами трио-ревью
Ф5-5b/5c; красный здесь = «появилась НОВАЯ неклассифицированная дверь».
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "sreda"

#: Примитивы, создающие сырую сессию/фабрику app-движка (мимо швов).
_PRIMITIVES = {"get_session_factory", "get_db_session", "sessionmaker"}

#: Шов сам — единственное место, где примитивы легитимно определяются/используются.
_SEAM_FILE = "db/session.py"


def _scan_doors() -> set[tuple[str, str, str]]:
    """(relpath, qualname-функции, примитив) для каждого вызова/референса."""
    doors: set[tuple[str, str, str]] = set()
    for py in sorted(SRC.rglob("*.py")):
        rel = py.relative_to(SRC).as_posix()
        if rel == _SEAM_FILE:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))

        # qualname через стек областей
        def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
            new_stack = stack
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                new_stack = stack + (node.name,)
            if isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute)
                    else None
                )
                if name in _PRIMITIVES:
                    doors.add((rel, ".".join(stack) or "<module>", name))
                # Depends(get_session/get_db_session) — роут на сырой сессии
                if name == "Depends":
                    for arg in node.args:
                        if isinstance(arg, ast.Name) and arg.id in (
                            "get_session", "get_db_session",
                        ):
                            doors.add((
                                rel, ".".join(stack) or "<module>",
                                f"Depends({arg.id})",
                            ))
            elif isinstance(node, ast.Name) and node.id == "get_db_session" \
                    and isinstance(node.ctx, ast.Load):
                doors.add((rel, ".".join(stack) or "<module>", "get_db_session"))
            for child in ast.iter_child_nodes(node):
                visit(child, new_stack)

        visit(tree, ())
    return doors


#: ЗАМОРОЖЕННЫЙ инвентарь дверей. Формат: (файл, функция, примитив): причина
#: «почему безопасно после флипа DSN». Источники: Ф2-карта
#: (plans/138-f2-remainder-map.md) + трио-ревью Ф5-5b/5c R1-R5.
_ALLOWED: dict[tuple[str, str, str], str] = {
    ("admin/auth.py", "_resolve_tg_principal", "get_session_factory"):
        "admin-auth #305: admin_sessions/admin_login_challenges = NO_RLS, "
        "app-гранты сохранены (0078 ревокал только audit_log/dashboard/alerts)",
    ("api/deps.py", "get_session", "get_db_session"):
        "определение легаси FastAPI-dep (реэкспорт get_db_session); использования "
        "классифицируются на сайтах Depends(...) отдельно",
    ("api/routes/connect.py", "open_eds_connect_form", "Depends(get_db_session)"):
        "#181 tombstone: EDS retired, роут возвращает статическую страницу, "
        "сессию НЕ использует",
    ("api/routes/connect.py", "open_eds_connect_form", "get_db_session"):
        "#181 tombstone (референс имени в Depends того же роута)",
    ("api/routes/connect.py", "submit_eds_connect_form", "Depends(get_db_session)"):
        "#181 tombstone: EDS retired, статическая страница, сессия не используется",
    ("api/routes/connect.py", "submit_eds_connect_form", "get_db_session"):
        "#181 tombstone (референс имени в Depends того же роута)",
    ("api/routes/miniapp.py", "_require_miniapp_auth", "Depends(get_session)"):
        "auth-фаза 5c: NO_RLS-чтения (runtime_config через guard); все tenant-"
        "операции внутри — под своими швами (резолв DEFINER, tenant_session gate/"
        "stamp/workspace, provision под identity). Ревью R3-R5",
    ("runtime/planner/plan_library.py", "_record_sync", "get_session_factory"):
        "легаси plan-execute (deprecated 2026-06-19, wildcard ReAct): путь мёртв "
        "на проде; при реанимации — шов (Ф2-карта)",
    ("runtime/planner_chat.py", "run_planner_chat_loop._persist_diag",
     "get_session_factory"):
        "легаси plan-execute диагностика: путь мёртв на проде (см. выше)",
    ("runtime/react_checkpoint_saver.py", "EncryptedSqlCheckpointSaver.__init__",
     "get_session_factory"):
        "saver._sf: put/put_writes исполняются в турне ПОД ctx (детач-ход ставит); "
        "cross-check ownership — отдельно под privileged('gc') (M9)",
    ("runtime/react_loop.py", "_run_post_turn_summary_inner", "get_session_factory"):
        "detached-пересказчик #232: спавнится create_task ИЗ турна под ctx → "
        "контекст копируется в задачу (механизм = test_setconfig_reemitted + "
        "R5-разбор ack-poll)",
    ("runtime/react_trace_persist.py", "_session", "get_session_factory"):
        "react_turn_trace: start/finish пишутся в handle_turn ПОД ctx турна",
    ("services/housewife_chat_tools.py", "build_housewife_tools.add_task",
     "get_session_factory"):
        "инструмент исполняется в ReAct-турне ПОД ctx (детач-ход)",
    ("services/housewife_chat_tools.py",
     "build_housewife_tools.link_task_to_checklist", "get_session_factory"):
        "инструмент исполняется в ReAct-турне ПОД ctx",
    ("services/housewife_chat_tools.py", "build_housewife_tools.unlink_task",
     "get_session_factory"):
        "инструмент исполняется в ReAct-турне ПОД ctx",
    ("services/llm.py", "_resolve_provider_overrides", "get_session_factory"):
        "runtime_config = NO_RLS (0082), app-грант сохранён",
    ("services/max_inbound.py", "_process_approved_max_turn", "get_session_factory"):
        "детач-ход: ставит СВОЙ tenant_ctx до любой tenant-операции, reset в finally",
    ("services/max_inbound.py", "_wait_outbox_delivered_for_tenant",
     "get_session_factory"):
        "ack-poll: спавнится create_task внутри ctx-блока → контекст скопирован "
        "(R5-субагент разобрал)",
    ("services/max_inbound.py", "handle_max_update", "get_session_factory"):
        "5c: пост-резолв хвост обёрнут в tenant_ctx (commit+set+try/finally); "
        "pre-ctx часть — только NO_RLS/privileged (резолв DEFINER, channel-link "
        "privileged)",
    ("services/telegram_inbound.py", "_process_approved_turn_locked",
     "get_session_factory"):
        "детач-ход: ставит СВОЙ tenant_ctx (:269), reset в outer-finally",
    ("services/telegram_inbound.py", "handle_telegram_update", "get_session_factory"):
        "5c: хвост обёрнут в tenant_ctx; queue-ветка под своим ctx (finally); "
        "pre-ctx — admin-login (NO_RLS) + резолв DEFINER",
    ("workers/job_runner.py", "process_pending_jobs_once", "get_session_factory"):
        "Ф2 срез-4: суб-сервисы сами открывают privileged-скан + tenant_session "
        "на строку; переданная session — легаси runtime/skill_platform "
        "(ActionRuntimeService=LEGACY, Ф2-карта; проверка на Ф5-репетиции)",
    ("workers/telegram_long_poll.py", "TelegramLongPoller.__init__",
     "get_session_factory"):
        "poller_offsets/poller_heartbeats = NO_RLS (0082); advisory-lock движок — "
        "allow-list Ф2-карты",
}


def test_no_unclassified_session_doors():
    doors = _scan_doors()
    unclassified = doors - set(_ALLOWED)
    ghosts = set(_ALLOWED) - doors
    assert not unclassified, (
        "НОВЫЕ двери к БД мимо швов (после флипа DSN упадут об RLS, если трогают "
        "tenant-таблицы без ctx). Либо используй tenant_session/privileged_session, "
        "либо классифицируй здесь с причиной:\n  " +
        "\n  ".join(str(d) for d in sorted(unclassified))
    )
    assert not ghosts, (
        "Мёртвые записи allow-list (дверь исчезла/переехала — обнови инвентарь):\n  "
        + "\n  ".join(str(g) for g in sorted(ghosts))
    )
