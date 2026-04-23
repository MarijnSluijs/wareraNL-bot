from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from .permissions import PanelRole, can_access


def get_session_user(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def get_session_user_or_redirect(request: Request) -> dict | RedirectResponse:
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    return user


def require_role(minimum: PanelRole):
    async def _guard(user: dict = Depends(get_session_user)) -> dict:
        try:
            current = PanelRole[user["panel_role"]]
        except Exception as exc:
            raise HTTPException(status_code=403, detail="Invalid panel role") from exc
        if not can_access(current, minimum):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _guard
