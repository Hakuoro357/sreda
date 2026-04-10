class SiteMonitorClient:
    """Legacy stub — superseded by ``sreda_feature_eds_monitor``.

    All methods raise ``NotImplementedError`` so any accidental import
    fails loudly instead of silently returning empty results.
    """

    async def ensure_authenticated_session(self, account_key: str) -> None:
        raise NotImplementedError("SiteMonitorClient is a legacy stub; use sreda_feature_eds_monitor")

    async def fetch_claims(self, account_key: str) -> list[dict]:
        raise NotImplementedError("SiteMonitorClient is a legacy stub; use sreda_feature_eds_monitor")

    async def fetch_claim_detail(self, account_key: str, claim_id: str) -> dict:
        raise NotImplementedError("SiteMonitorClient is a legacy stub; use sreda_feature_eds_monitor")
