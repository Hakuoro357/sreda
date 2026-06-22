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
from sreda.runtime.handlers import ActionRuntimeError

# #181 Phase B: the EDS billing-state gates (subscription.add_eds /
# eds.connect.* / claim.lookup) that used to read ``context["billing_summary"]``
# are gone with the retired skill. Those action types now fall straight through
# to their tombstoned handlers (each returns "Это умение отключено."). The only
# remaining structured precondition is the runtime-context presence check that
# every authenticated action shares.


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

    return None
