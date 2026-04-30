"""deactivate voice_transcription standalone subscription plan

Voice transcription is no longer a standalone paid skill. It's a
capability bundled with agents whose manifest declares
``includes_voice=True`` (currently Помощник домохозяйки; Тимлид будet
next). Spec conversation on 2026-04-18.

What this migration does:

  * Flips ``subscription_plans.voice_transcription_base`` to
    ``is_public=False`` and ``is_active=False`` — it disappears from
    the catalog (``/api/v1/plans`` and the Mini App subscriptions
    screen) and from ``start_simple_subscription`` flows. The row
    itself stays for historical integrity (FK from old tenant rows).
  * Leaves any ``TenantSubscription`` rows on this plan untouched.
    Voice access now flows through the agent-capability gate in
    ``services.agent_capabilities.has_voice_access`` which never looks
    at these rows anyway (it checks ``includes_voice`` on the
    subscribed agents' manifests).

Downgrade restores ``is_public=True, is_active=True`` so the plan
re-appears in the catalog. Does NOT re-add the plan if it was deleted
(not this migration's job).

Revision ID: 20260418_0018
Revises: 20260417_0017
Create Date: 2026-04-18
"""

from __future__ import annotations

from alembic import op

revision = "20260418_0018"
down_revision = "20260417_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE subscription_plans
           SET is_public = FALSE,
               is_active = FALSE,
               updated_at = CURRENT_TIMESTAMP
         WHERE plan_key = 'voice_transcription_base'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE subscription_plans
           SET is_public = TRUE,
               is_active = TRUE,
               updated_at = CURRENT_TIMESTAMP
         WHERE plan_key = 'voice_transcription_base'
        """
    )
