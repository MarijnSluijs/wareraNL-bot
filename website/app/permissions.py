from __future__ import annotations

from enum import IntEnum

from .config import Settings


class PanelRole(IntEnum):
    analyst = 10
    moderator = 20
    admin = 30
    owner = 40


def resolve_panel_role(discord_user_id: str, settings: Settings) -> PanelRole | None:
    if discord_user_id in settings.panel_owner_ids:
        return PanelRole.owner
    if discord_user_id in settings.panel_admin_ids:
        return PanelRole.admin
    if discord_user_id in settings.panel_moderator_ids:
        return PanelRole.moderator
    if discord_user_id in settings.panel_analyst_ids:
        return PanelRole.analyst
    return None


def role_name(role: PanelRole) -> str:
    return role.name.capitalize()


def can_access(role: PanelRole, minimum: PanelRole) -> bool:
    return role >= minimum
