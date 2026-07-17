"""Shared loader for the rotating WarEra API key list.

Reads the path from RW_API_KEYS_PATH (set per-service in the various
docker-compose*.yml files so discord-bot, data-fetcher and rijksoverheid-web
each get a dedicated slice of keys and can't rate-limit each other). Falls
back to the shared _api_keys.json when the dedicated file hasn't been set up
on a given machine yet — e.g. a standalone deployment that only runs one of
these services.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("discord_bot")

_FALLBACK = "_api_keys.json"


def load_api_keys() -> list[str]:
    keys_path = os.getenv("RW_API_KEYS_PATH", _FALLBACK)
    candidates = [keys_path] if keys_path == _FALLBACK else [keys_path, _FALLBACK]

    for candidate in candidates:
        try:
            with open(candidate, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        keys = data.get("keys", []) if isinstance(data, dict) else data
        if keys:
            if candidate != keys_path:
                logger.warning(
                    "API keys file %s not found — fell back to %s", keys_path, candidate
                )
            return [str(k) for k in keys]

    logger.debug("No API keys file found at %s, continuing without API keys", keys_path)
    return []
