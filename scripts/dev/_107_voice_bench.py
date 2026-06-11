"""#107 — бенч рта в изоляции: одно сырьё → разные модели, замер времени.

Честный дизайн: НИКАКОГО планировщика — фиксированные пары (запрос, сырьё)
уходят напрямую в DEFAULT_LLM_COMPOSER (humanize_result) при разных
SREDA_COMPOSER_PROVIDER. Сравниваются только «рты».
"""
import io, os, sys, time, base64, tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
os.environ["SREDA_DATABASE_URL"] = f"sqlite:///{(Path(tempfile.gettempdir())/'vb107.db').as_posix()}"
os.environ["SREDA_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()
os.environ["SREDA_MIMO_API_KEY_FILE"] = str(_REPO / ".secrets" / "mimo_api_key.txt")
os.environ["SREDA_OPENROUTER_API_KEY_FILE"] = str(_REPO / ".secrets" / "openrouter-token.md")
os.environ["SREDA_TG_ACCOUNT_SALT"] = "x"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROVIDER = sys.argv[1]
TONE = sys.argv[2] if len(sys.argv) > 2 else None  # #126: тон персоны
if TONE is not None and TONE not in ("warm_practical", "tender_care"):
    sys.exit(f"неизвестный тон: {TONE!r} (warm_practical | tender_care)")
os.environ["SREDA_COMPOSER_PROVIDER"] = PROVIDER
from sreda.config.settings import get_settings
get_settings.cache_clear()

from sreda.services.composer.llm_composer import DEFAULT_LLM_COMPOSER
from sreda.services.composer.compose import ComposerContext


class _Log:  # минимальный execution_log для агрегата
    outcome = "completed"
    steps = []


BIG_LIST = (
    "В списке покупок:\n\nМолочные:\n• молоко\n• Сливочное масло (200 г)\n\n"
    "Мясо и рыба:\n• куриное филе (1 упаковка)\n\nОвощи и фрукты:\n• банан (1 шт)\n"
    "• картофель (1 кг)\n• помидоры (1 шт)\n• огурец (1 шт)\n• морковь (1 шт)\n"
    "• лук репчатый (1 шт)\n• Зелень укропа (1 пучок)\n\nХлеб:\n• хлеб\n\n"
    "Бакалея:\n• лапша (1 пачка)\n• мёд (1 банка)\n• овсяные хлопья (1 пачка)\n\n"
    "Замороженное:\n• котлеты куриные (2 шт)\n\nДругое:\n• яйца"
)

CASES = [
    ("покажи список покупок", BIG_LIST),
    ("добавь ещё хлеб и кефир",
     "Добавила: кефир. Уже было в списке (не дублирую): хлеб."),
    ("удали куриное филе из списка", "Убрала из списка покупок: куриное филе."),
    ("добавь задачу позвонить сантехнику завтра",
     "Создана задача «Позвонить сантехнику» на завтра (без напоминания)."),
    ("отметь что я купила молоко и хлеб",
     "Отметила купленным: молоко, хлеб. Осталось в списке: кефир, картофель."),
]

ctx = ComposerContext(tenant_id="bench", run_id="bench", user_message="",
                      locale="ru-RU", timezone="Europe/Moscow",
                      persona_preset=TONE)
print(f"### ПРОВАЙДЕР: {PROVIDER} ТОН: {TONE or 'дефолт'}")
for i, (intent, raw) in enumerate(CASES, 1):
    t0 = time.monotonic()
    try:
        res = DEFAULT_LLM_COMPOSER(
            llm_prompt_key="humanize_result",
            template_data={"intent": intent,
                           "actions": [{"user_visible_summary": raw, "status": "ok"}]},
            execution_log=_Log(), ctx=ctx,
        )
        dt = time.monotonic() - t0
        print(f"\n--- КЕЙС {i} «{intent}» [{dt:.1f}с] ---")
        print(res.text.strip()[:900])
    except Exception as exc:
        print(f"\n--- КЕЙС {i} «{intent}» СБОЙ: {type(exc).__name__}: {str(exc)[:150]}")
