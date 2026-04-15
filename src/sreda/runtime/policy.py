"""Policy-guard: structured preconditions for action execution.

Extracted from ``ActionRuntimeService._policy_guard`` so the graph's
``policy_guard`` node can be a thin wrapper that just dispatches here.
``evaluate_policy`` returns ``None`` when the action passes, or an
``ActionRuntimeError`` describing why it failed. Raising from a node is
awkward in LangGraph (aborts the whole graph), so we return it and let
the graph route to ``persist_error``."""

from __future__ import annotations

from typing import Any

from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.handlers import (
    ActionRuntimeError,
    connect_reply_markup,
    status_subscriptions_markup,
    subscriptions_markup,
)
from sreda.services.claim_lookup import is_valid_claim_id
from sreda.services.onboarding import build_connect_eds_message


def evaluate_policy(
    action: ActionEnvelope, context: dict[str, Any]
) -> ActionRuntimeError | None:
    # ``help.show`` is always allowed — it's the unauthenticated entry point.
    if action.action_type == "help.show":
        return None

    if not context.get("tenant_id") or not context.get("workspace_id"):
        return ActionRuntimeError(
            "runtime_context_missing",
            "Не удалось определить контекст пользователя для этого действия.",
        )

    summary = context["billing_summary"]

    if action.action_type == "claim.lookup":
        claim_id = str(action.params.get("claim_id") or "").strip()
        if not claim_id:
            return ActionRuntimeError(
                "claim_id_missing",
                "Используй команду в формате:\n\n/claim <номер_заявки>",
                reply_markup=status_subscriptions_markup(),
            )
        if not is_valid_claim_id(claim_id):
            return ActionRuntimeError(
                "claim_id_invalid",
                "Номер заявки должен содержать только буквы, цифры, '-' или '_'.",
                reply_markup=status_subscriptions_markup(),
            )
        if not context.get("eds_monitor_enabled"):
            return ActionRuntimeError(
                "eds_monitor_disabled",
                "Поиск по заявкам станет доступен после подключения EDS.",
                reply_markup=subscriptions_markup(),
            )
        return None

    if action.action_type == "subscription.add_eds" and not summary["base_active"]:
        return ActionRuntimeError(
            "subscription_required",
            "Сначала подключи EDS Monitor, а потом можно будет добавить еще один кабинет.",
            reply_markup=subscriptions_markup(),
        )

    if action.action_type in {"eds.connect.start", "eds.connect.retry"}:
        if not summary["base_active"]:
            return ActionRuntimeError(
                "subscription_required",
                build_connect_eds_message(
                    base_active=False,
                    connected_count=summary["connected_count"],
                    allowed_count=summary["allowed_count"],
                ),
                reply_markup=connect_reply_markup(False),
            )
        if summary["free_count"] <= 0:
            return ActionRuntimeError(
                "limit_exceeded",
                "Сейчас все оплаченные кабинеты уже заняты.\n\n"
                "Если нужен еще один кабинет, сначала добавь его в подписках.",
                reply_markup=subscriptions_markup(),
            )

    return None
