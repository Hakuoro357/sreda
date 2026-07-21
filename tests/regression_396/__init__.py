"""#396 — replay/регрессионный харнесс надёжности с проверкой СОСТОЯНИЯ БД.

Мерит, ЧИНЯТ ли фиксы реально: на каждый живой баг — фикстура
{начальное состояние БД + история диалога + ожидаемые/запрещённые мутации
+ конечное состояние + потолок confirm/tool-вызовов}. Судим по ИНВАРИАНТАМ
состояния (``invariants.py``), а не «ответ похож».

Переиспользует проверенный паттерн:
  * ``tests/unit/conftest.py`` — фикстуры ``db_session`` (свежая sqlite + rollback),
    ``seed_telegram_user`` (Tenant/Workspace/User/Profile), autouse env/checkpointer;
  * ``tests/unit/test_post_tool_reply_393.py`` — ``_StubLLM`` (детерминированный
    драйвер модели: скрипт из ``AIMessage``, ``bind_tools→self``, ``invoke→next``)
    + реальный ``react_loop.handle_turn`` end-to-end;
  * ``plans/orch/qwen-cycle-eval-harness/qwen_cycle_eval.py`` — паттерн
    ``db_counts`` before/after + судьи-по-состоянию + ``text_quality``.
"""
