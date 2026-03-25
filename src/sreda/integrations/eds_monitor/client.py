class SiteMonitorClient:
    async def ensure_authenticated_session(self, account_key: str) -> None:
        # TODO: rewrite current Playwright-based integration in Python.
        _ = account_key

    async def fetch_claims(self, account_key: str) -> list[dict]:
        _ = account_key
        return []

    async def fetch_claim_detail(self, account_key: str, claim_id: str) -> dict:
        _ = (account_key, claim_id)
        return {}
