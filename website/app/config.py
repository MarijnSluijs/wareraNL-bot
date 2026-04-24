from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_name: str = "WarEraNL Admin Panel"
    session_secret: str = "change-me"
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    discord_redirect_uri: str | None = None
    bot_db_path: str = "database/external.db"
    bot_log_path: str = "logs/discord.log"
    bot_config_path: str = "config/config.json"
    audit_log_path: str = "website/data/panel_audit.jsonl"
    panel_owner_ids: tuple[str, ...] = ()
    panel_admin_ids: tuple[str, ...] = ()
    panel_moderator_ids: tuple[str, ...] = ()
    panel_analyst_ids: tuple[str, ...] = ()

    @property
    def oauth_enabled(self) -> bool:
        return bool(
            self.discord_client_id
            and self.discord_client_secret
            and self.discord_redirect_uri
        )


def _csv_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _load_config_owner_ids(path: str) -> tuple[str, ...]:
    config_path = Path(path)
    if not config_path.exists():
        return ()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    owner_ids = data.get("owner_ids") or []
    return tuple(str(owner_id) for owner_id in owner_ids)


def load_settings() -> Settings:
    config_path = os.getenv("BOT_CONFIG_PATH", "config/config.json")
    owner_ids = set(_load_config_owner_ids(config_path))
    owner_ids.update(_csv_ids(os.getenv("PANEL_OWNER_IDS")))

    return Settings(
        session_secret=os.getenv("PANEL_SESSION_SECRET", "change-me"),
        discord_client_id=os.getenv("DISCORD_CLIENT_ID"),
        discord_client_secret=os.getenv("DISCORD_CLIENT_SECRET"),
        discord_redirect_uri=os.getenv("DISCORD_REDIRECT_URI"),
        bot_db_path=os.getenv("BOT_DB_PATH", "database/external.db"),
        bot_log_path=os.getenv("BOT_LOG_PATH", "logs/discord.log"),
        bot_config_path=config_path,
        audit_log_path=os.getenv("PANEL_AUDIT_LOG_PATH", "website/data/panel_audit.jsonl"),
        panel_owner_ids=tuple(sorted(owner_ids)),
        panel_admin_ids=_csv_ids(os.getenv("PANEL_ADMIN_IDS")),
        panel_moderator_ids=_csv_ids(os.getenv("PANEL_MODERATOR_IDS")),
        panel_analyst_ids=_csv_ids(os.getenv("PANEL_ANALYST_IDS")),
    )
