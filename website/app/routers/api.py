from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..deps import require_role
from ..permissions import PanelRole

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/dashboard/overview")
async def dashboard_overview(
    request: Request,
    _: dict = Depends(require_role(PanelRole.analyst)),
    days: int = 7,
):
    days = max(1, min(days, 30))
    return await request.app.state.data_service.dashboard_overview(days=days)


@router.get("/automation/tasks")
async def automation_tasks(
    request: Request,
    _: dict = Depends(require_role(PanelRole.analyst)),
):
    return {"tasks": await request.app.state.data_service.task_health()}


@router.get("/community/users")
async def community_users(
    request: Request,
    _: dict = Depends(require_role(PanelRole.moderator)),
    q: str = "",
):
    return {"users": await request.app.state.data_service.users(search=q)}


@router.get("/community/moderation")
async def community_moderation(
    request: Request,
    _: dict = Depends(require_role(PanelRole.moderator)),
):
    return {"cases": await request.app.state.data_service.moderation_cases()}


@router.get("/system/logs")
async def system_logs(
    request: Request,
    _: dict = Depends(require_role(PanelRole.analyst)),
    level: str = "ALL",
    van: str = "",
    tot: str = "",
):
    return {
        "logs": await request.app.state.data_service.logs(
            level=level,
            limit=250,
            van=van,
            tot=tot,
        )
    }


@router.get("/system/audit")
async def system_audit(
    request: Request,
    _: dict = Depends(require_role(PanelRole.admin)),
):
    return {"entries": await request.app.state.data_service.audit_entries(limit=300)}
