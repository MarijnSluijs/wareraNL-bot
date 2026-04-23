from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..deps import get_session_user_or_redirect
from ..permissions import PanelRole

router = APIRouter(tags=["pages"])


def _ctx(user: dict, **extra):
    return {
        "user": user,
        "section": extra.pop("section", "dashboard"),
        **extra,
    }


@router.get("/")
async def dashboard_page(request: Request):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    overview = await request.app.state.data_service.dashboard_overview(days=7)
    task_health = await request.app.state.data_service.task_health()
    economy = await request.app.state.data_service.economy_snapshot()
    await request.app.state.data_service.audit(str(user["id"]), "page.view", {"page": "dashboard"})
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            user,
            section="dashboard",
            overview=overview,
            task_health=task_health[:20],
            economy=economy,
        ),
    )


@router.get("/community/users")
async def users_page(request: Request, q: str = ""):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    users = await request.app.state.data_service.users(search=q)
    await request.app.state.data_service.audit(str(user["id"]), "page.view", {"page": "community.users", "q": q})
    return request.app.state.templates.TemplateResponse(
        request,
        "users.html",
        _ctx(user, section="community", users=users, q=q),
    )


@router.get("/community/moderation")
async def moderation_page(request: Request, status: str = "all"):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    cases = await request.app.state.data_service.moderation_cases(status_filter=status)
    await request.app.state.data_service.audit(
        str(user["id"]),
        "page.view",
        {"page": "community.moderation", "status": status},
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "moderation.html",
        _ctx(user, section="community", cases=cases, status=status),
    )


@router.get("/config")
async def config_page(request: Request):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    role = PanelRole[user["panel_role"]]
    if role < PanelRole.admin:
        return request.app.state.templates.TemplateResponse(
            request,
            "forbidden.html",
            _ctx(user, section="config"),
            status_code=403,
        )

    config = await request.app.state.data_service.guild_config()
    await request.app.state.data_service.audit(str(user["id"]), "page.view", {"page": "config"})
    return request.app.state.templates.TemplateResponse(
        request,
        "config.html",
        _ctx(user, section="config", config=config),
    )


@router.get("/automation")
async def automation_page(request: Request):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    task_health = await request.app.state.data_service.task_health()
    await request.app.state.data_service.audit(str(user["id"]), "page.view", {"page": "automation"})
    return request.app.state.templates.TemplateResponse(
        request,
        "automation.html",
        _ctx(user, section="automation", task_health=task_health),
    )


@router.get("/system/logs")
async def logs_page(request: Request, level: str = "ALL"):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    logs = await request.app.state.data_service.logs(level=level, limit=250)
    await request.app.state.data_service.audit(
        str(user["id"]), "page.view", {"page": "system.logs", "level": level}
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "logs.html",
        _ctx(user, section="system", logs=logs, level=level),
    )


@router.get("/system/audit")
async def audit_page(request: Request):
    user = get_session_user_or_redirect(request)
    if isinstance(user, RedirectResponse):
        return user

    entries = await request.app.state.data_service.audit_entries(limit=300)
    await request.app.state.data_service.audit(str(user["id"]), "page.view", {"page": "system.audit"})
    return request.app.state.templates.TemplateResponse(
        request,
        "audit.html",
        _ctx(user, section="system", entries=entries),
    )
