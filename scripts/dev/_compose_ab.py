"""Оценка Mercury в роли РТА (LLM-композер). Изолированно: прямой вызов
_compose_with_llm на 6 зарегистрированных типах ответа × 2 персоны
(warm_practical=None и tender_care). Без планировщика и пакета фич.
Провайдер берётся из SREDA_COMPOSER_PROVIDER."""
import base64, io, os, sys, tempfile, time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("SREDA_COMPOSER_PROVIDER", "inception-mercury2")
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir())/'composeab.db').as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ["SREDA_INCEPTION_API_KEY_FILE"] = str(_REPO/".secrets"/"inception.txt")
os.environ["SREDA_OPENROUTER_API_KEY_FILE"] = str(_REPO/".secrets"/"openrouter-token.md")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

from sreda.config.settings import get_settings; get_settings.cache_clear()
from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.services.composer.compose import ComposerContext
from sreda.services.composer.llm_composer import make_llm_composer

_compose = make_llm_composer()


def _log(outcome, *statuses):
    steps = tuple(StepResult(step_id=f"s{i+1}", tool="t", status=st)
                  for i, st in enumerate(statuses or ("ok",)))
    return ExecutionLog(steps=steps, outcome=outcome)


# (key, user_message, template_data, execution_log) — основные «выходы» рта
SCENARIOS = [
    ("recipe_narrative", "покажи рецепт борща", {
        "recipe_title": "Борщ",
        "ingredients": ["свёкла 2 шт", "капуста 300 г", "картошка 3 шт",
                        "морковь 1 шт", "лук 1 шт", "томатная паста 2 ст.л."],
        "steps": ["Сварить мясной бульон 40 минут",
                  "Добавить нашинкованную капусту и картошку",
                  "Спассеровать свёклу с морковью, луком и томатной пастой",
                  "Соединить, варить ещё 15 минут, дать настояться"],
        "source": "интернет",
    }, _log("completed")),

    ("recipe_added_to_shopping_narrative", "добавь продукты для сырников", {
        "recipe_title": "Сырники",
        "added_items": ["творог 400 г", "яйца 2 шт", "мука 4 ст.л."],
        "duplicates": ["сахар"],
    }, _log("completed")),

    ("multi_action_summary", "добавь молоко и хлеб, напомни завтра позвонить маме и удали старый список дел", {
        "actions": [
            {"status": "ok", "user_visible_summary": "добавила в покупки: молоко, хлеб"},
            {"status": "ok", "user_visible_summary": "поставила напоминание «позвонить маме» на завтра 9:00"},
            {"status": "error", "user_visible_summary": "не удалось удалить список дел «на дачу» — список не найден"},
        ],
    }, _log("partial_failure", "ok", "ok", "error")),

    ("cooking_explanation", "сколько варить свёклу для борща?", {
        "question": "сколько варить свёклу для борща?",
        "facts": ["Свёклу для борща варят 40–50 минут до мягкости",
                  "Для насыщенного цвета свёклу можно запечь 1 час при 200 °C",
                  "Готовность проверяют ножом — должен входить легко"],
    }, _log("completed")),

    ("smalltalk", "привет! как настроение?", {
        "user_message": "привет! как настроение?",
    }, _log("completed")),

    ("humanize_result", "запиши в покупки молоко и творог", {
        "intent": "добавить в покупки молоко и творог",
        "actions": [{"status": "ok", "user_visible_summary": "добавила в покупки: молоко, творог"}],
    }, _log("completed")),
]

PERSONAS = [("warm_practical (дефолт)", None), ("tender_care", "tender_care")]


def main():
    s = get_settings()
    print(f"### РОТ A/B | composer_provider={s.composer_provider}\n")
    for key, msg, data, log in SCENARIOS:
        print("=" * 78)
        print(f"[{key}]  запрос: «{msg}»")
        for label, preset in PERSONAS:
            ctx = ComposerContext(user_message=msg, persona_preset=preset)
            t0 = time.monotonic()
            try:
                r = _compose(llm_prompt_key=key, template_data=data,
                             execution_log=log, ctx=ctx)
                dt = time.monotonic() - t0
                print(f"\n  --- {label} ({dt:.1f}с, {r.model}) ---")
                for line in r.text.strip().splitlines():
                    print(f"  {line}")
            except Exception as e:
                print(f"\n  --- {label} → ОШИБКА: {type(e).__name__}: {str(e)[:200]}")
        print()


main()
