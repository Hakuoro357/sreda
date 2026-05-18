"""R-39 День 1: эмпирический замер трёх моделей через ``get_chat_llm``.

Проверяем что три кандидата для новой архитектуры действительно
работают с нашим продакшен-стэком (OpenRouter+LangChain ChatOpenAI,
прокси MimoChatOpenAI):

1. **gemini-3.1-flash-lite** (предполагаемый новый основной): дешёвая
   модель с подтверждённым tool_calling и json_schema (по результатам
   Day 0 пробников).
2. **deepseek-v4-flash** (запасной 1): альтернативная серия с
   совместимыми возможностями.
3. **mimo-v2.5-pro** (запасной 2 с zero-trust замком): нынешний
   основной, после R-39 переходит в роль резерва.

Сценарии:

- *Болтовня*: «Как дела?» — должно прийти tool_calls=[] + тёплый
  ответ на русском.
- *Действие*: «Поставь напоминание на завтра 9 утра потягать
  гантели.» — ожидаем tool_call schedule_reminder.
- *Коррекция*: два хода. Turn 1 «Поставь напоминание на 2 часа
  разбудить Катю», Turn 2 «Нет, неправильное, поставь на 14:00
  разбудить Катю.» — ожидаем что на Turn 2 модель не молча отвечает,
  а вызывает либо replace_reminder, либо cancel+schedule.

Запуск (на VDS, под виртэнвом со всеми переменными среды):

    cd /opt/sreda && python -m scripts.r39_probe_three_models

Результат — ``plans/r39-day1-probe-results.json`` с raw-данными +
``stdout`` со сводкой.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Чтобы запускался и из ``scripts/`` и через ``python -m scripts.r39_probe_three_models``
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


from langchain_core.tools import tool

from sreda.services.llm import get_chat_llm


# ─── Минимальные заглушки инструментов ────────────────────────────────


@tool
def schedule_reminder(title: str, trigger_iso: str) -> str:
    """Поставить одноразовое напоминание. ``trigger_iso`` в UTC (Europe/Moscow = UTC+3)."""
    return f"OK: schedule_reminder('{title}', '{trigger_iso}')"


@tool
def cancel_reminder(reminder_id: str) -> str:
    """Отменить одно напоминание по идентификатору."""
    return f"OK: cancel_reminder('{reminder_id}')"


@tool
def replace_reminder(reminder_id: str, title: str, trigger_iso: str) -> str:
    """Заменить существующее напоминание новым временем/заголовком атомарно."""
    return f"OK: replace_reminder('{reminder_id}', '{title}', '{trigger_iso}')"


_TOOLS = [schedule_reminder, cancel_reminder, replace_reminder]


# ─── Конфиг кандидатов ────────────────────────────────────────────────


@dataclass
class ModelCandidate:
    label: str
    provider: str
    model: str


_CANDIDATES = [
    ModelCandidate(
        label="gemini-3.1-flash-lite",
        provider="openrouter",
        model="google/gemini-3.1-flash-lite",
    ),
    ModelCandidate(
        label="deepseek-v4-flash",
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
    ),
    ModelCandidate(
        label="mimo-v2.5-pro",
        provider="mimo-v2.5",
        model="mimo-v2.5-pro",
    ),
]


_SYSTEM_PROMPT = (
    "Ты Среда — тёплый домашний ассистент. Отвечай по-русски, кратко и "
    "по-доброму. Когда пользователь явно просит выполнить действие "
    "(поставить/отменить/изменить напоминание), обязательно вызывай "
    "соответствующий инструмент в том же ответе. Не сочиняй id — "
    "если нужен идентификатор для отмены, проси у пользователя или "
    "используй контекст беседы."
)


# ─── Сценарии ─────────────────────────────────────────────────────────


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    description: str
    turns: list[str]  # последовательность user сообщений (assistant выводит модель)
    expected_tool: str | None  # имя ожидаемого инструмента на последнем ходу
    expected_no_tool: bool = False  # ожидаем пустой tool_calls


_SCENARIOS = [
    Scenario(
        name="chitchat",
        description="Болтовня — tool_calls=[] + тёплый ответ на русском",
        turns=["Как дела?"],
        expected_tool=None,
        expected_no_tool=True,
    ),
    Scenario(
        name="mutation",
        description="Простое действие — schedule_reminder",
        turns=["Поставь напоминание на завтра 9 утра потягать гантели."],
        expected_tool="schedule_reminder",
    ),
    Scenario(
        name="correction",
        description="Коррекция — replace_reminder или cancel+schedule на втором ходу",
        turns=[
            "Поставь напоминание на 2 часа разбудить Катю.",
            "Нет, неправильное, поставь на 14:00 разбудить Катю.",
        ],
        expected_tool="schedule_reminder",  # или replace_reminder; проверка allow-list
    ),
]


# ─── Прогон одной модели ──────────────────────────────────────────────


@dataclass
class ScenarioResult:
    scenario: str
    turns: list[Turn]
    latency_seconds: float
    error: str | None = None


def _extract_tool_calls(message: Any) -> list[dict[str, Any]]:
    """LangChain AIMessage → нормализованный список tool_calls."""
    raw = getattr(message, "tool_calls", None) or []
    out: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            out.append({"name": tc.get("name"), "args": tc.get("args")})
        else:
            out.append({
                "name": getattr(tc, "name", None),
                "args": getattr(tc, "args", None),
            })
    return out


def run_scenario(llm_with_tools: Any, scenario: Scenario) -> ScenarioResult:
    """Прогоняет один сценарий через готовый LLM-клиент."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    messages: list[Any] = [SystemMessage(_SYSTEM_PROMPT)]
    turns_log: list[Turn] = []
    started = time.perf_counter()
    try:
        for user_text in scenario.turns:
            messages.append(HumanMessage(user_text))
            turns_log.append(Turn(role="user", content=user_text))
            ai = llm_with_tools.invoke(messages)
            messages.append(ai)
            turns_log.append(Turn(
                role="assistant",
                content=str(getattr(ai, "content", "") or ""),
                tool_calls=_extract_tool_calls(ai),
            ))
    except Exception as exc:  # noqa: BLE001 — собираем любой сбой провайдера
        return ScenarioResult(
            scenario=scenario.name,
            turns=turns_log,
            latency_seconds=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}",
        )
    return ScenarioResult(
        scenario=scenario.name,
        turns=turns_log,
        latency_seconds=time.perf_counter() - started,
    )


def run_one_model(candidate: ModelCandidate) -> dict[str, Any]:
    """Все сценарии для одной модели."""
    print(f"\n=== {candidate.label} ({candidate.provider}: {candidate.model}) ===")
    llm = get_chat_llm(
        provider=candidate.provider,
        model=candidate.model,
        temperature=0.3,
    )
    if llm is None:
        print(f"  ! модель не построилась (нет ключа для {candidate.provider})")
        return {
            "candidate": asdict(candidate),
            "built": False,
            "scenarios": [],
        }
    llm_with_tools = llm.bind_tools(_TOOLS)
    results: list[ScenarioResult] = []
    for scenario in _SCENARIOS:
        print(f"  > сценарий: {scenario.name}")
        r = run_scenario(llm_with_tools, scenario)
        results.append(r)
        if r.error:
            print(f"    !! ошибка: {r.error.splitlines()[0]}")
        else:
            last = r.turns[-1] if r.turns else None
            tc_names = [tc.get("name") for tc in (last.tool_calls if last else [])]
            text = (last.content if last else "")[:120].replace("\n", " ")
            print(f"    tool_calls: {tc_names}")
            print(f"    text:       {text!r}")
            print(f"    latency:    {r.latency_seconds:.2f}s")
    return {
        "candidate": asdict(candidate),
        "built": True,
        "scenarios": [
            {
                **asdict(r),
                "turns": [asdict(t) for t in r.turns],
            }
            for r in results
        ],
    }


def _verdict_for_scenario(
    scenario: Scenario, result_payload: dict[str, Any]
) -> tuple[bool, str]:
    """Простая эвристика прохождения сценария."""
    if result_payload.get("error"):
        return False, "error"
    turns = result_payload.get("turns") or []
    if not turns:
        return False, "no_turns"
    last = turns[-1]
    tc_names = {tc.get("name") for tc in (last.get("tool_calls") or [])}
    if scenario.expected_no_tool:
        return (not tc_names), ("ok" if not tc_names else f"unexpected_tools:{tc_names}")
    # mutation/correction → нужен хотя бы один из допустимых инструментов
    allowed = {"schedule_reminder", "replace_reminder", "cancel_reminder"}
    overlap = tc_names & allowed
    if not overlap:
        return False, f"no_action_tool:{tc_names or 'empty'}"
    return True, f"ok:{sorted(overlap)}"


def main() -> int:
    out_path = _REPO_ROOT / "plans" / "r39-day1-probe-results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": [],
    }
    for candidate in _CANDIDATES:
        try:
            payload["models"].append(run_one_model(candidate))
        except Exception as exc:  # noqa: BLE001 — выводим в payload, не падаем
            payload["models"].append({
                "candidate": asdict(candidate),
                "built": False,
                "fatal_error": f"{type(exc).__name__}: {exc}",
            })

    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nrezultat sokhranen: {out_path}")

    # Сводка
    print("\n=== Сводка ===")
    print(f"{'model':28s} {'scenario':14s} {'verdict':40s}")
    for model_payload in payload["models"]:
        cand = model_payload.get("candidate") or {}
        label = cand.get("label", "?")
        if not model_payload.get("built"):
            print(f"{label:28s} {'N/A':14s} {'построение_не_удалось':40s}")
            continue
        for scenario, sr in zip(_SCENARIOS, model_payload["scenarios"]):
            ok, note = _verdict_for_scenario(scenario, sr)
            mark = "+" if ok else "-"
            print(f"{label:28s} {scenario.name:14s} {mark + ' ' + note:40s}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
