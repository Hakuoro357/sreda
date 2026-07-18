from __future__ import annotations

import logging
from importlib import import_module

from sreda.domain.tenants.features import is_feature_disabled
from sreda.features.registry import FeatureRegistry

logger = logging.getLogger(__name__)


def load_feature_modules(module_paths: list[str], registry: FeatureRegistry) -> None:
    """Import and register feature modules from ``SREDA_FEATURE_MODULES``.

    2026-07-18 audit (deploy-ops MAJOR): a broken module (stale import of a
    retired feature package left in env, import error, registration error)
    must NOT abort startup — otherwise a deploy with a stale module entry
    turns into a full outage (uvicorn never comes up). Each module loads in
    isolation: failure → error log + admin alert + skip; the remaining
    modules keep loading.
    """
    for module_path in module_paths:
        try:
            _load_one(module_path, registry)
        except Exception:  # noqa: BLE001
            logger.exception(
                "feature module %r failed to load; skipped (startup continues)",
                module_path,
            )
            # Best-effort operator alert; alerting must never break loading.
            try:
                from sreda.services.admin_alerts import send_admin_alert

                send_admin_alert(
                    "P1",
                    f"Feature module failed to load: {module_path}",
                    f"import/registration of feature module {module_path!r} raised; "
                    "the module was SKIPPED, startup continues without it. "
                    "Check SREDA_FEATURE_MODULES and the traceback in logs.",
                    dedupe_key=f"feature_module_load:{module_path}",
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "admin alert for failed feature module not sent",
                    exc_info=True,
                )


def _load_one(module_path: str, registry: FeatureRegistry) -> None:
    module = import_module(module_path)

    # #181 defense-in-depth prefilter: a module that exposes a
    # ``feature_module`` for a retired skill is skipped before any
    # registration side-effect. (FeatureRegistry.register also guards;
    # this stops the import-driven ``register()`` path from wiring API
    # routes / runtime / workers for a disabled feature.)
    feature_key = getattr(getattr(module, "feature_module", None), "feature_key", None)
    if feature_key is not None and is_feature_disabled(feature_key):
        return

    if hasattr(module, "register"):
        module.register(registry)
        return

    feature_module = getattr(module, "feature_module", None)
    if feature_module is not None:
        registry.register(feature_module)
        return

    raise RuntimeError(f"Feature module '{module_path}' does not expose register() or feature_module")
