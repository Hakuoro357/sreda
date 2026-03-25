from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.get("")
async def list_approvals() -> dict[str, list]:
    return {"items": []}
