"""MAX Bot API integration package.

Mirrors `sreda.integrations.telegram` shape: ``MaxClient`` httpx wrapper
+ shared exception class. Phase 2 of MAX integration sprint, plan
``plans/max-integration-webhook-miniapp-final.md``.
"""

from sreda.integrations.max.client import MaxClient, MaxDeliveryError

__all__ = ["MaxClient", "MaxDeliveryError"]
