"""#150: рендер шаблона /admin/spend-by-model (jinja-ошибки + ключевые поля)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sreda.admin.queries import SpendModelRow, SpendReport
from sreda.admin.routes import templates

UTC = timezone.utc


def _report() -> SpendReport:
    return SpendReport(
        period="month",
        start_utc=datetime(2026, 6, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 1, tzinfo=UTC),
        rows=[
            SpendModelRow("inception-mercury2", "mercury-2", 10, 40000, 600,
                          Decimal("0.0500"), Decimal("0.1200"), True),
            SpendModelRow("mimo-v2.5-pro", "mimo-v2.5-pro", 3, 500, 50,
                          None, None, False),
        ],
        priced_subtotal_usd=Decimal("0.0500"),
        upper_subtotal_usd=Decimal("0.1200"),
        unpriced_models=[("mimo-v2.5-pro", "mimo-v2.5-pro")],
        unpriced_calls=3, unpriced_tokens=550,
        coverage_calls_pct=77, coverage_tokens_pct=99, anomaly_count=1,
    )


def test_spend_by_model_template_renders() -> None:
    html = templates.env.get_template("spend_by_model.html").render(
        token="tok", section="spend-by-model", period="month", report=_report(),
    )
    assert "Расходы по моделям" in html
    assert "mercury-2" in html                 # priced строка
    assert "$0.0500" in html                   # priced subtotal / est
    assert "—" in html                         # беспрайсовая показана прочерком
    assert "Покрытие" in html and "77%" in html
    assert "Аномальных" in html                # сноска аномалий
    assert "текущему прайсу" in html           # дисклеймер


def test_spend_by_model_template_empty() -> None:
    empty = SpendReport(
        period="day", start_utc=datetime(2026, 6, 1, tzinfo=UTC),
        end_utc=datetime(2026, 6, 2, tzinfo=UTC), rows=[],
        priced_subtotal_usd=Decimal("0"), upper_subtotal_usd=Decimal("0"),
        unpriced_models=[], unpriced_calls=0, unpriced_tokens=0,
        coverage_calls_pct=None, coverage_tokens_pct=None, anomaly_count=0,
    )
    html = templates.env.get_template("spend_by_model.html").render(
        token="tok", section="spend-by-model", period="day", report=empty,
    )
    assert "Нет вызовов за период" in html
