from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


def _require_authenticated_actor() -> None:
    """Fail-closed authentication placeholder for the approvals route.

    The approvals endpoint is a stub today but is already reachable at a
    public URL. Until a real authentication layer lands we refuse every
    request so a future PR that starts returning real data cannot
    accidentally leak it through this open route.
    """

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication_required",
    )


@router.get("", dependencies=[Depends(_require_authenticated_actor)])
async def list_approvals() -> dict[str, list]:
    return {"items": []}
