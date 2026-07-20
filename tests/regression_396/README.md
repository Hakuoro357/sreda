# Replay/регрессионный харнесс надёжности (#396)

Мерит, **чинят ли фиксы реально**. На каждый живой баг — фикстура
{начальное состояние БД + скрипт диалога + ожидаемые/запрещённые мутации +
конечное состояние + потолок confirm/tool-вызовов}. Прогон через **реальный**
`react_loop.handle_turn` на свежей sqlite. Судим по **ИНВАРИАНТАМ СОСТОЯНИЯ**, а не
«ответ похож»: состояние снимается **логической проекцией** `{таблица: {id: (status,
title)}}` (скоуп tenant+user) — ловит in-place мутации (archive/complete/rename), не
только дельту count; плюс per-receipt классификация квитанций (applied / already_applied
/ failed / noop / read), текст реплики и потолки.

Детерминированно и офлайн: модель заменена скриптованным `StubLLM` (без сети,
без стоимости, без флейков) → пригодно как гейт перед релизом.

## Как запустить

```bash
cd C:/pro/sreda-wt-396
PYTHONPATH="C:/pro/sreda-wt-396/src" \
  <venv>/python.exe -m pytest tests/regression_396/ -q
# <venv> — любой venv с зависимостями sreda, напр. C:/pro/vex-assistant/sreda/.venv/Scripts
# PYTHONPATH обязателен: тестируем КОД ЭТОГО worktree (иначе editable-install затенит).
```

Ожидаемо: `passed` + ровно **1 xfailed** (открытый баг #390 — красный до фикса).

## Инварианты (`invariants.py`)

Пять из issue #396. Универсальные (1,2,4a,5) гоняются на каждой фикстуре;
контекстные (3,4) — по декларации фикстуры.

| # | Инвариант | Функция | Ловит |
|---|-----------|---------|-------|
| 1 | ответ.успех ⇒ эффект применён (мутация БД / applied-квитанция) | `inv_no_false_success` | ложный успех (#376/#393) |
| 2 | эффект.failed ⇒ ответ не «готово» | `inv_failed_not_done` | рапорт успеха поверх упавшего инструмента |
| 3 | тот же idempotency_key ⇒ ≤1 мутация | `inv_idempotency` | дубли, сломанный дедуп |
| 4 | неоднозначная отмена ⇒ 0 мутаций | `inv_ambiguous_cancel_zero_mutations` | удаление наугад |
| 4a | мутация состоялась ⇒ ответ не «отменила» | `inv_mutated_not_cancelled` | ложная отмена (#362) |
| 5 | публичный ответ ∩ тех-поля = ∅ | `inv_public_no_tech` | техвыдача (#390/g-075) |

Инварианты — **чистые функции** над `DialogOutcome`; тестируются на синтетике
в `test_invariants.py` (RED-тесты: каждый инвариант ловит нарушение).

## Фикстуры (`fixtures.py`)

| Фикстура | Баг | Что проверяет | Статус |
|----------|-----|---------------|--------|
| `393_add_filler` | #393 закрыт | add + филлер → реплика называет список+пункты, мутация 3 пункта | зелёная |
| `393_archive_resume_dump` | #393 закрыт | archive через confirm → «да» → реплика называет «Дача», не дамп; список archived | зелёная |
| `389_shopping_add_no_confirm` | #389 закрыт | add в покупки БЕЗ встречного confirm (потолок=0), 2 позиции | зелёная |
| `362_direct_write_not_cancelled` | #362 открыт | запись сахара → мутация есть, реплика не «отменила» (инв.4a) | зелёная (guard) |
| `synthetic_idempotency` | инв.3 | повтор add молока ⇒ дедуп, итог 1 позиция | зелёная |
| `synthetic_ambiguous_cancel` | инв.4 | «отмени» без цели ⇒ 0 мутаций | зелёная |
| `390_show_lists_tech_leak` | #390 открыт | receipt-contract: сырой вывод `get_checklist(overview)` = англ. «pending/done/total» (источник, реальный код; reply скриптован чистым) | **xfail** (красный) |

## Режущая способность (`test_cutting_power.py`)

Доказываем, что «зелёные» фикстуры не пустые:

* **#393**: выключаем kill-switch фикса (`SREDA_REACT_POST_TOOL_REPORT=0`) →
  фикстура **краснеет** (филлер доходит до юзера), при этом мутация РЕАЛЬНО
  состоялась (баг про отчёт, не про действие). Прод-код не трогаем.
* **#390**: `xfail(strict=True)` — красный на текущем main по техвыдаче в
  СЫРОМ выводе инструмента (реальный код, не скрипт). Починят формат →
  XPASS (падение strict-xfail) = сигнал «снять маркер, сделать жёстким ассертом».

## Как добавить фикстуру нового бага

1. В `fixtures.py`: `Fixture(id=..., bug=..., seed=..., turns=[ScriptedTurn(...)], <ожидания>)`.
   Ходы модели скриптуются `ai_tool(name, args)` / `ai_text(text)`.
2. Ожидания декларативны: `expect_mutations` / `forbid_mutations` /
   `require_phrases` / `forbid_phrases` / `max_confirms` / `idempotent_group` /
   `ambiguous_cancel_turns` / `require_clean_receipts` / `expect_final`.
3. Добавь в `GREEN_FIXTURES` (ждём зелёную) или `OPEN_BUG_FIXTURES` (открытый
   баг → xfail до фикса).

## Переиспользованное

* `tests/unit/conftest.py` — `db_session`, `seed_telegram_user`, autouse-фикстуры.
* `tests/unit/test_post_tool_reply_393.py` — паттерн `_StubLLM`.
* `plans/orch/qwen-cycle-eval-harness/qwen_cycle_eval.py` — паттерн `db_counts`
  before/after + судьи-по-состоянию.
* `sreda.runtime.react_result_report` — `collect_successful_writes` /
  `reply_grounds_result` / `reply_has_tech_leak` (эффекты + примитивы инвариантов).
