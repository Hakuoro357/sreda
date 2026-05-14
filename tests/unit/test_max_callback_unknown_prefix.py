"""R-19 regression: MAX callback unknown prefix sync drop.

Production incident 2026-05-12 18:58 UTC — 2 callback events для
tenant_max_76425659 застряли с processing_status='ingested' навечно
→ monitor alert «unprocessed_inbound».

Корень: MAX webhook echo'ит bot's own outbound message (с inline-
кнопками) обратно как message_callback events с opaque MAX-generated
payload tokens. Эти tokens не имеют наших known prefix'ов
(btn_reply:/pb:/rem_done:/rem_snooze:). При unknown prefix:
- `_handle_max_callback` возвращает False
- Caller спавнит `_process_approved_max_turn` background task
- Task должна set 'processing_started' и в итоге 'ignored' (через
  dispatch_max_action → None)
- НО если task не start'ит (FastAPI BackgroundTasks race / asyncio
  loop close) — status НИКОГДА не updates → stuck в 'ingested'

Fix: sync detection unknown callback prefix через
`_is_unknown_max_callback_prefix` helper + mark 'ignored' до spawn'а.
"""

from __future__ import annotations

from sreda.services.max_inbound import (
    _KNOWN_MAX_CALLBACK_PREFIXES,
    _is_unknown_max_callback_prefix,
    _max_callback_payload,
)


# --------------------------------------------------------------------
# Known prefixes — must continue to be routed (NOT treated as unknown)
# --------------------------------------------------------------------


def test_known_btn_reply_is_not_unknown() -> None:
    """btn_reply:<token> routed by max_inbound._handle_max_callback /
    btn_reply handler в outer scope."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": "btn_reply:abcd1234"},
    }
    assert _is_unknown_max_callback_prefix(payload) is False


def test_known_pb_is_not_unknown() -> None:
    """pb:<branch> routed by pending_bot tour navigation."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": "pb:intro"},
    }
    assert _is_unknown_max_callback_prefix(payload) is False


def test_known_rem_done_is_not_unknown() -> None:
    """rem_done:<id> routed by FamilyReminder state update."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": "rem_done:42"},
    }
    assert _is_unknown_max_callback_prefix(payload) is False


def test_known_rem_snooze_is_not_unknown() -> None:
    """rem_snooze:<id>:<minutes> reminder snooze callback."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": "rem_snooze:42:60"},
    }
    assert _is_unknown_max_callback_prefix(payload) is False


def test_channel_link_callbacks_known() -> None:
    """confirm_link:/cancel_link: handled earlier в handle_max_update."""
    for cb_data in ("confirm_link:tok_xyz", "cancel_link:tok_xyz"):
        payload = {
            "update_type": "message_callback",
            "callback": {"payload": cb_data},
        }
        assert _is_unknown_max_callback_prefix(payload) is False, (
            f"{cb_data} should be known"
        )


# --------------------------------------------------------------------
# Unknown prefixes — must be detected (incident reproduction)
# --------------------------------------------------------------------


def test_unknown_opaque_token_detected() -> None:
    """Production incident 2026-05-12: MAX echo callback с opaque
    payload tokens like `f9LHodD0cOJdRG2hCmdBy0oLEsHWEu9jx43Tt...`."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": "f9LHodD0cOJdRG2hCmdBy0oLEsHWEu9jx43TtjThScqe"},
    }
    assert _is_unknown_max_callback_prefix(payload) is True


def test_unknown_random_prefix_detected() -> None:
    """Generic unknown prefix should be flagged."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": "future_feature:42"},
    }
    assert _is_unknown_max_callback_prefix(payload) is True


def test_missing_callback_payload_treated_as_unknown() -> None:
    """message_callback БЕЗ callback.payload — degenerate, treat as
    unknown → safe drop."""
    payload = {
        "update_type": "message_callback",
        "callback": {},  # no payload key
    }
    assert _is_unknown_max_callback_prefix(payload) is True


def test_non_string_callback_payload_treated_as_unknown() -> None:
    """callback.payload not a string (e.g. dict) — treat as unknown."""
    payload = {
        "update_type": "message_callback",
        "callback": {"payload": {"unexpected": "shape"}},
    }
    assert _is_unknown_max_callback_prefix(payload) is True


# --------------------------------------------------------------------
# Non-callback update_types — must NOT be treated as unknown callback
# --------------------------------------------------------------------


def test_message_created_is_not_callback() -> None:
    """Regular text message — pass through (not a callback at all)."""
    payload = {
        "update_type": "message_created",
        "message": {"body": {"text": "Привет"}},
    }
    assert _is_unknown_max_callback_prefix(payload) is False


def test_bot_started_is_not_callback() -> None:
    """bot_started event — pass through."""
    payload = {"update_type": "bot_started"}
    assert _is_unknown_max_callback_prefix(payload) is False


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def test_max_callback_payload_extraction() -> None:
    """`_max_callback_payload` helper returns the string или None."""
    p1 = {"update_type": "message_callback", "callback": {"payload": "btn_reply:x"}}
    assert _max_callback_payload(p1) == "btn_reply:x"

    p2 = {"update_type": "message_created"}
    assert _max_callback_payload(p2) is None

    p3 = {"update_type": "message_callback", "callback": {}}
    assert _max_callback_payload(p3) is None


def test_known_prefixes_constant_includes_all_handlers() -> None:
    """_KNOWN_MAX_CALLBACK_PREFIXES must include each handler's prefix.

    Partial-revert guard — если кто-то удалит prefix из tuple или
    handler из routing.

    Xiaomi r1 MAJOR: assert FULL expected set, не subset — иначе
    removal `confirm_link:`/`cancel_link:` проскочило бы guard.
    """
    expected = {
        "btn_reply:",     # _handle_max_callback btn_reply handler (outer scope)
        "pb:",            # pending_bot tour navigation
        "rem_done:",      # FamilyReminder mark done
        "rem_snooze:",    # FamilyReminder snooze
        "confirm_link:",  # channel-link confirmation (handle_max_update line 104)
        "cancel_link:",   # channel-link cancellation (handle_max_update line 114)
    }
    actual = set(_KNOWN_MAX_CALLBACK_PREFIXES)
    assert actual == expected, (
        f"_KNOWN_MAX_CALLBACK_PREFIXES drift:\n"
        f"  missing: {expected - actual}\n"
        f"  unexpected: {actual - expected}\n"
        f"If adding new handler — update both `_KNOWN_MAX_CALLBACK_PREFIXES` "
        f"tuple AND this test's `expected` set."
    )
    # Length sanity — full match check (set comparison above уже implies
    # equal cardinality для unique-element tuples).
    assert len(_KNOWN_MAX_CALLBACK_PREFIXES) == len(expected)
