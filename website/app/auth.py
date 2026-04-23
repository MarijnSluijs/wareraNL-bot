from __future__ import annotations

from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import Settings
from .permissions import role_name, resolve_panel_role

router = APIRouter(prefix="/auth", tags=["auth"])
oauth = OAuth()


def init_oauth(settings: Settings) -> None:
    if not settings.oauth_enabled:
        return

    oauth.register(
        name="discord",
        client_id=settings.discord_client_id,
        client_secret=settings.discord_client_secret,
        authorize_url="https://discord.com/api/oauth2/authorize",
        access_token_url="https://discord.com/api/oauth2/token",
        api_base_url="https://discord.com/api/",
        client_kwargs={"scope": "identify"},
    )


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    settings = request.app.state.settings
    if not settings.oauth_enabled:
        # Local fallback for development when OAuth env is absent.
        default_id = (settings.panel_owner_ids or settings.panel_admin_ids or ("0",))[0]
        request.session["user"] = {
            "id": default_id,
            "username": "Local Dev",
            "avatar": None,
            "panel_role": "owner" if default_id in settings.panel_owner_ids else "admin",
        }
        return RedirectResponse(url="/", status_code=302)

    redirect_uri = settings.discord_redirect_uri
    discord = oauth.create_client("discord")
    if discord is None:
        raise HTTPException(status_code=500, detail="Discord OAuth is not configured")
    return await discord.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    discord = oauth.create_client("discord")
    if discord is None:
        raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

    token: dict[str, Any] = await discord.authorize_access_token(request)
    resp = await discord.get("users/@me", token=token)
    user = resp.json()

    user_id = str(user["id"])
    role = resolve_panel_role(user_id, request.app.state.settings)
    if role is None:
        raise HTTPException(status_code=403, detail="No panel role assigned")

    request.session["user"] = {
        "id": user_id,
        "username": user.get("username", "unknown"),
        "avatar": user.get("avatar"),
        "panel_role": role.name,
        "panel_role_label": role_name(role),
    }
    await request.app.state.data_service.audit(
        actor_id=user_id,
        action="auth.login",
        details={"username": user.get("username", "unknown"), "role": role.name},
    )

    return RedirectResponse(url="/", status_code=302)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    user = request.session.get("user")
    if user:
        await request.app.state.data_service.audit(
            actor_id=str(user.get("id", "unknown")),
            action="auth.logout",
            details={"username": user.get("username", "unknown")},
        )
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)
