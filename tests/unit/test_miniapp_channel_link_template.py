from __future__ import annotations

from pathlib import Path


SUBSCRIPTIONS_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "sreda"
    / "miniapp"
    / "templates"
    / "subscriptions.html"
)


def _template() -> str:
    return SUBSCRIPTIONS_TEMPLATE.read_text(encoding="utf-8")


def test_confirm_consume_failure_uses_inline_error_without_alert() -> None:
    template = _template()

    assert "window._consumeChannelLink(token, {silent: true})" in template
    assert "window._consumeChannelLink = function(rawToken, opts)" in template
    assert "var silent = !!(opts && opts.silent);" in template
    consume_start = template.index("window._consumeChannelLink = function(rawToken, opts)")
    consume_body = template[consume_start:template.index("window._cancelChannelLink", consume_start)]
    gated_alert = "if (!silent) showAlert(_channelLinkLastErrorMessage);"

    assert consume_body.count(gated_alert) == 3
    assert "showAlert(" not in consume_body.replace(gated_alert, "")


def test_cancel_resets_confirm_view_before_close_fallback() -> None:
    template = _template()

    cancel_start = template.index("window._cancelChannelLink = function(rawToken)")
    cancel_body = template[cancel_start:template.index("window._subscribeSkill", cancel_start)]
    first_reset = cancel_body.index("_channelLinkView = null;")
    first_close = cancel_body.index("closeMiniAppOrClear();")

    assert first_reset < first_close


def test_success_view_has_continue_button_back_to_dashboard() -> None:
    template = _template()

    assert 'id="channel-link-continue-btn"' in template
    assert "Продолжить" in template
    assert "bindChannelLinkSuccess()" in template
    assert "continueBtn.addEventListener" in template
    assert "_channelLinkView = null;" in template
