"""#285 Фаза 0: базлайн — доля write-исполнений без детерминированного сигнала.

Гоняется НА ПРОДЕ (нужны env-ключи шифрования + БД): офлайн-переигрывание детекторов по
`react_turn_trace.origin_user_text` + `tool_calls_json` (план chatfact-unify-final, Фаза 0).
Выводит ТОЛЬКО агрегаты — никаких текстов пользователей.

Детекторы:
- old  = сегодняшний сигнал #197/#215: `_must_task` | `_section_hint` (сравнительное поле).
- v0   = old + командные глаголы + декларативные stable-fact паттерны
         (стартовые корпуса: plans/285-signal-corpora-v0.md).

Оговорки (план, Фаза 0): confirm-ходы считаются ОТДЕЛЬНО (resume-неполнота tool_calls_json,
#269 react_loop._turn_outcome); предусловие SREDA_REACT_TRACE_ENABLED=1 проверено снаружи.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import select

from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.session import get_session_factory
from sreda.runtime.react_preflight import _must_task, _section_hint
from sreda.services.tool_schemas.families import TOOL_OP_CLASS

# v0-расширение: командные глаголы (корпус 1.1) — намеренно щедрые корни, это ЗАМЕР recall,
# не боевой гейт (боевой лексикон калибруется в Фазе B по этому же базлайну).
_CMD = re.compile(
    r"\b(добав|сохран|запиш|заплан|внес|отмет|вычеркн|постав|удал|напомн|создай|отмен|перенес)"
    r"[а-яё]*",
    re.IGNORECASE,
)
# v0 декларативы (корпус 1.2): high-precision формы; «меня зовут на дачу» отсечено требованием
# заглавной буквы после «зовут» (near-miss класс).
_DECL = [
    re.compile(r"\bменя зовут\s+[А-ЯЁ][а-яё]+"),
    re.compile(r"\b(я\s+)?живу в\s+[А-ЯЁ]"),
    re.compile(r"\bу меня (двое|трое|четверо|\d+)\s*(дет|реб|сын|доч)"),
    re.compile(r"\b(мужа|жену|сына|дочь|дочку|кота|собаку)\s+зовут\s+[А-ЯЁ]"),
]


def _old_signal(text: str) -> bool:
    return bool(_must_task(text)) or (_section_hint(text) is not None)


def _v0_signal(text: str) -> bool:
    return _old_signal(text) or bool(_CMD.search(text)) or any(p.search(text) for p in _DECL)


def main() -> None:
    with get_session_factory()() as s:
        rows = s.execute(
            select(ReactTurnTrace).where(ReactTurnTrace.status == "done")
        ).scalars().all()

    total = fresh = confirm_turns = confirm_w_write = 0
    fresh_w_write = 0
    unsig = {"old": 0, "v0": 0}
    unsig_tools: Counter[str] = Counter()
    read_own_on_unsig = 0
    web = {"web_search", "fetch_url", "get_weather"}

    for r in rows:
        total += 1
        try:
            tcs = json.loads(r.tool_calls_json or "[]")
        except (TypeError, ValueError):
            tcs = []
        writes = [
            c for c in tcs
            if TOOL_OP_CLASS.get(str(c.get("name"))) == "write" and c.get("result_kind") == "ok"
        ]
        if (r.confirm_state or "none") != "none":
            confirm_turns += 1
            if writes:
                confirm_w_write += 1
            continue  # #269: в основную метрику не идут
        fresh += 1
        if not writes:
            continue
        fresh_w_write += 1
        text = r.origin_user_text or ""
        if not _old_signal(text):
            unsig["old"] += 1
        if not _v0_signal(text):
            unsig["v0"] += 1
            for c in writes:
                unsig_tools[str(c.get("name"))] += 1
            for c in tcs:
                nm = str(c.get("name"))
                if (
                    TOOL_OP_CLASS.get(nm) == "read_pure"
                    and nm not in web
                    and c.get("result_kind") == "ok"
                ):
                    read_own_on_unsig += 1

    def share(x: int) -> str:
        return f"{x}/{fresh_w_write}" if fresh_w_write else "0/0"

    print(f"done turns total: {total}")
    print(f"  fresh (confirm_state=none): {fresh}; confirm turns: {confirm_turns} "
          f"(of them with executed writes: {confirm_w_write} — считать отдельно, #269)")
    print(f"fresh turns with executed writes: {fresh_w_write}")
    print(f"GO/NO-GO: write-ходы БЕЗ сигнала — old(#197/#215): {share(unsig['old'])}; "
          f"v0(+команды+декларативы): {share(unsig['v0'])}")
    print(f"own-data reads (ok) на v0-БЕЗсигнальных write-ходах: {read_own_on_unsig}")
    print("top write-инструменты на v0-БЕЗсигнальных ходах (имя: счёт):")
    for name, cnt in unsig_tools.most_common(10):
        print(f"  {name}: {cnt}")


if __name__ == "__main__":
    main()
