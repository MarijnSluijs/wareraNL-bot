"""
Configuration loader for the WarEra bot.

Configs live in config/ (preferred) with a fallback to the project root for
backward compatibility.  All bot code accesses config via ``bot.config`` — use
this module only in ``bot.py`` during startup.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("discord_bot")

# Ordered search paths: config/ folder first, then project root as fallback.
_SEARCH_DIRS: list[Path] = [Path("config"), Path(".")]


def find_config(name: str) -> Path:
    """Return the first existing path for *name* across search dirs.

    If no file is found, returns ``config/<name>`` (the canonical new location)
    so callers get a sensible error message.
    """
    for d in _SEARCH_DIRS:
        p = d / name
        if p.exists():
            return p
    return Path("config") / name


def load_config(config_path: str | Path | None = None) -> dict:
    """Load and return the bot config dict.

    *config_path* can be:
    - ``None``               → use ``config/config.json`` (with root fallback)
    - ``"testing"``          → use ``config/testing_config.json`` (with root fallback)
    - any explicit path      → use that path directly
    """
    if config_path is None:
        path = find_config("config.json")
    elif str(config_path) == "testing":
        path = find_config("testing_config.json")
    else:
        path = Path(config_path)
        # If given a bare filename with no directory, search the standard dirs
        if not path.parent.parts or str(path.parent) == ".":
            path = find_config(path.name)

    try:
        with path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        logger.info("Configuration loaded from %s", path)
        return cfg
    except Exception as e:
        logger.error("Failed to load config %s: %s", path, e)
        return {
            "colors": {
                "primary": "0x154273",
                "success": "0x57F287",
                "error": "0xE02B2B",
                "warning": "0xF59E42",
            }
        }
