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

#: Отслеживаемые примитивы: {(модуль, имя): каноничное-имя-примитива}.
#: R1-353 (Codex medium MAJOR): алиасы РЕЗОЛВЯТСЯ через import-карту файла —
#: `from sqlalchemy.orm import Session as _SASession` тоже ловится.
_TRACKED_IMPORTS: dict[tuple[str, str], str] = {
    ("sreda.db.session", "get_session_factory"): "get_session_factory",
    ("sreda.db.session", "get_db_session"): "get_db_session",
    ("sreda.db.session", "get_engine"): "engine",
    ("sreda.db.session", "get_app_engine"): "engine",
    ("sreda.db.session", "get_maintenance_engine"): "engine",
    ("sreda.db.session", "get_identity_engine"): "engine",
    ("sqlalchemy.orm", "sessionmaker"): "sessionmaker",
    ("sqlalchemy.orm", "Session"): "Session",
    ("sqlalchemy", "create_engine"): "create_engine",
}

#: Шов сам — единственное место, где примитивы легитимно определяются/используются.
_SEAM_FILE = "db/session.py"


def _scan_doors() -> dict[tuple[str, str, str], int]:
    """{(relpath, qualname-функции, примитив): счётчик вызовов}.

    R1-353 (Codex medium MAJOR): значение — СЧЁТЧИК, не факт присутствия:
    вторая дверь в уже разрешённой функции меняет счётчик → красный."""
    doors: dict[tuple[str, str, str], int] = {}

    def bump(rel: str, stack: tuple[str, ...], prim: str) -> None:
        key = (rel, ".".join(stack) or "<module>", prim)
        doors[key] = doors.get(key, 0) + 1

    for py in sorted(SRC.rglob("*.py")):
        rel = py.relative_to(SRC).as_posix()
        if rel == _SEAM_FILE:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))

        # карта алиасов файла: локальное-имя → каноничный примитив
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    prim = _TRACKED_IMPORTS.get((node.module, a.name))
                    if prim is not None:
                        aliases[a.asname or a.name] = prim
        # прямые имена работают и без импорта в этом файле (защита от
        # `import sreda.db.session as dbs; dbs.get_session_factory()`)
        for (_mod, name), prim in _TRACKED_IMPORTS.items():
            aliases.setdefault(name, prim)

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
                prim = aliases.get(name or "")
                if prim == "engine":
                    # сами геттеры движка не дверь; дверь — .connect()/.begin()
                    # на них: get_engine().connect() (raw connection мимо швов)
                    pass
                elif prim is not None:
                    bump(rel, stack, prim)
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in ("connect", "begin")
                    and isinstance(fn.value, ast.Call)
                ):
                    inner = fn.value.func
                    iname = (
                        inner.id if isinstance(inner, ast.Name)
                        else inner.attr if isinstance(inner, ast.Attribute)
                        else None
                    )
                    if aliases.get(iname or "") == "engine":
                        bump(rel, stack, f"engine.{fn.attr}")
                # Depends(get_session/get_db_session) — роут на сырой сессии;
                # и позиционная, и keyword-форма (dependency=...)
                if name == "Depends":
                    dep_args = list(node.args) + [kw.value for kw in node.keywords]
                    for arg in dep_args:
                        if isinstance(arg, ast.Name) and arg.id in (
                            "get_session", "get_db_session",
                        ):
                            bump(rel, stack, f"Depends({arg.id})")
            for child in ast.iter_child_nodes(node):
                visit(child, new_stack)

        visit(tree, ())
    return doors


#: ЗАМОРОЖЕННЫЙ инвентарь дверей. Формат: (файл, функция, примитив):
#: (ожидаемое-число-вызовов, причина «почему безопасно после флипа DSN»).
#: R1-353 (Codex medium/high MAJOR): счётчик — вторая дверь в разрешённой
#: функции меняет число → красный. Источники классификаций: Ф2-карта
#: (plans/138-f2-remainder-map.md) + трио-ревью Ф5-5b/5c R1-R5.
_ALLOWED: dict[tuple[str, str, str], tuple[int, str]] = {
    ('admin/auth.py', '_resolve_tg_principal', 'get_session_factory'):
        (1,
         "admin-auth #305: admin_sessions/admin_login_challenges = NO_RLS, "
         "app-гранты сохранены (0078 ревокал только "
         "audit_log/dashboard/alerts)"
         ),
    ('api/deps.py', 'get_session', 'get_db_session'):
        (1,
         "определение легаси FastAPI-dep (реэкспорт get_db_session); "
         "использования классифицируются на сайтах Depends(...) отдельно"
         ),
    ('api/routes/connect.py', 'open_eds_connect_form', 'Depends(get_db_session)'):
        (1,
         "#181 tombstone: EDS retired, роут возвращает статическую страницу, "
         "сессию НЕ использует"
         ),
    ('api/routes/connect.py', 'submit_eds_connect_form', 'Depends(get_db_session)'):
        (1,
         "#181 tombstone: EDS retired, статическая страница, сессия не "
         "используется"
         ),
    ('api/routes/miniapp.py', '_require_miniapp_auth', 'Depends(get_session)'):
        (1,
         "auth-фаза 5c: NO_RLS-чтения (runtime_config через guard); все "
         "tenant-операции внутри — под своими швами (резолв DEFINER, "
         "tenant_session gate/stamp/workspace, provision под identity). Ревью "
         "R3-R5"
         ),
    ('runtime/planner/plan_library.py', '_record_sync', 'get_session_factory'):
        (1,
         "легаси plan-execute (deprecated 2026-06-19, wildcard ReAct): путь "
         "мёртв на проде; при реанимации — шов (Ф2-карта)"
         ),
    ('runtime/planner_chat.py', '_record_usage_safe', 'Session'):
        (1,
         "биллинг usage_ledger: новая Session на bind ТОГО ЖЕ движка, "
         "вызывается в турне ПОД ctx — begin-событие движка скоупит каждую txn "
         "(R1-353 v2: алиас _SASession пойман резолвом импортов)"
         ),
    ('runtime/planner_chat.py', 'run_planner_chat_loop._persist_diag', 'get_session_factory'):
        (1,
         "легаси plan-execute диагностика: путь мёртв на проде (см. выше)"
         ),
    ('runtime/react_checkpoint_saver.py', 'EncryptedSqlCheckpointSaver.__init__', 'get_session_factory'):
        (1,
         "saver._sf: put/put_writes исполняются в турне ПОД ctx (детач-ход "
         "ставит); cross-check ownership — отдельно под privileged('gc') (M9)"
         ),
    ('runtime/react_loop.py', '_record_react_usage', 'Session'):
        (1,
         "биллинг usage_ledger в ReAct-турне ПОД ctx (см. planner_chat; алиас "
         "_SASession)"
         ),
    ('runtime/react_loop.py', '_run_post_turn_summary_inner', 'get_session_factory'):
        (1,
         "detached-пересказчик #232: спавнится create_task ИЗ турна под ctx → "
         "контекст копируется в задачу (механизм = test_setconfig_reemitted + "
         "R5-разбор ack-poll)"
         ),
    ('runtime/react_trace_persist.py', '_session', 'get_session_factory'):
        (1,
         "react_turn_trace: start/finish пишутся в handle_turn ПОД ctx турна"
         ),
    ('services/db_health.py', 'database_is_ready', 'engine.connect'):
        (1,
         "health-проба SELECT 1 — tenant-таблиц не трогает"
         ),
    ('services/housewife_chat_tools.py', 'build_housewife_tools.add_task', 'get_session_factory'):
        (1,
         "инструмент исполняется в ReAct-турне ПОД ctx (детач-ход)"
         ),
    ('services/housewife_chat_tools.py', 'build_housewife_tools.link_task_to_checklist', 'get_session_factory'):
        (1,
         "инструмент исполняется в ReAct-турне ПОД ctx"
         ),
    ('services/housewife_chat_tools.py', 'build_housewife_tools.unlink_task', 'get_session_factory'):
        (1,
         "инструмент исполняется в ReAct-турне ПОД ctx"
         ),
    ('services/llm.py', '_resolve_provider_overrides', 'get_session_factory'):
        (1,
         "runtime_config = NO_RLS (0082), app-грант сохранён"
         ),
    ('services/max_inbound.py', '_process_approved_max_turn', 'get_session_factory'):
        (1,
         "детач-ход: ставит СВОЙ tenant_ctx до любой tenant-операции, reset в "
         "finally"
         ),
    ('services/max_inbound.py', '_wait_outbox_delivered_for_tenant', 'get_session_factory'):
        (1,
         "ack-poll: спавнится create_task внутри ctx-блока → контекст "
         "скопирован (R5-субагент разобрал)"
         ),
    ('services/max_inbound.py', 'handle_max_update', 'get_session_factory'):
        (1,
         "5c: пост-резолв хвост обёрнут в tenant_ctx (commit+set+try/finally); "
         "pre-ctx часть — только NO_RLS/privileged (резолв DEFINER, channel- "
         "link privileged)"
         ),
    ('services/telegram_inbound.py', '_process_approved_turn_locked', 'get_session_factory'):
        (1,
         "детач-ход: ставит СВОЙ tenant_ctx (:269), reset в outer-finally"
         ),
    ('services/telegram_inbound.py', 'handle_telegram_update', 'get_session_factory'):
        (1,
         "5c: хвост обёрнут в tenant_ctx; queue-ветка под своим ctx (finally); "
         "pre-ctx — admin-login (NO_RLS) + резолв DEFINER"
         ),
    ('services/tenant_lifecycle.py', '_get_lock_engine', 'create_engine'):
        (1,
         "advisory-lock движок (NullPool): только pg_advisory_lock-функции, "
         "таблиц не читает (Ф2-карта, allow-list)"
         ),
    ('workers/job_runner.py', 'process_pending_jobs_once', 'get_session_factory'):
        (1,
         "Ф2 срез-4: суб-сервисы сами открывают privileged-скан + "
         "tenant_session на строку; переданная session — легаси "
         "runtime/skill_platform (ActionRuntimeService=LEGACY, Ф2-карта; "
         "проверка на Ф5-репетиции)"
         ),
    ('workers/telegram_long_poll.py', 'TelegramLongPoller.__init__', 'get_session_factory'):
        (1,
         "poller_offsets/poller_heartbeats = NO_RLS (0082); advisory-lock "
         "движок — allow-list Ф2-карты"
         ),
    ('workers/telegram_long_poll.py', 'TelegramLongPoller.startup', 'create_engine'):
        (1,
         "advisory-lock движок поллера: session-scoped pg_advisory_lock, "
         "таблиц не читает (Ф2-карта: telegram_long_poll:165)"
         ),
}


def test_no_unclassified_session_doors():
    doors = _scan_doors()
    unclassified = {
        k: n for k, n in doors.items()
        if k not in _ALLOWED or n != _ALLOWED[k][0]
    }
    ghosts = set(_ALLOWED) - set(doors)
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
