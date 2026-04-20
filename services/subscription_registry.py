"""Central registry for user-facing subscription types.

Each subscription cog calls ``register()`` in its ``setup()`` function.
The ``/overzicht`` command reads ``all_entries()`` — no changes needed there
when a new subscription type is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class SubscriptionEntry:
    name: str
    emoji: str
    label: str
    get_fn: Callable[[Any, str], Awaitable[dict | None]]
    remove_fn: Callable[[Any, str], Awaitable[None]]
    detail_fn: Callable[[dict], str]


_registry: list[SubscriptionEntry] = []


def register(
    *,
    name: str,
    emoji: str,
    label: str,
    get_fn: Callable[[Any, str], Awaitable[dict | None]],
    remove_fn: Callable[[Any, str], Awaitable[None]],
    detail_fn: Callable[[dict], str],
) -> None:
    """Register a subscription type. Safe to call multiple times (idempotent)."""
    if any(e.name == name for e in _registry):
        return
    _registry.append(SubscriptionEntry(
        name=name,
        emoji=emoji,
        label=label,
        get_fn=get_fn,
        remove_fn=remove_fn,
        detail_fn=detail_fn,
    ))


def all_entries() -> list[SubscriptionEntry]:
    return list(_registry)


def get_entry(name: str) -> SubscriptionEntry | None:
    return next((e for e in _registry if e.name == name), None)
