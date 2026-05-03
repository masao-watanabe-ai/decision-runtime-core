"""Authentication dependency for FastAPI endpoints.

When auth_enabled=False (default), get_actor() returns None and all callers
fall back to body-supplied actor_id.

When auth_enabled=True, the X-Api-Key header is required.  The key is looked up
in settings.api_key_role_map; absent or unrecognised keys raise HTTP 401.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from backend.app.config import settings
from backend.app.models.actor import Actor


def get_actor(request: Request) -> Actor | None:
    """Return the authenticated Actor, or None when auth is disabled.

    Raises:
        HTTPException(401): When auth_enabled=True and the key is missing or invalid.
    """
    if not settings.auth_enabled:
        return None

    api_key = request.headers.get("X-Api-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-Api-Key header is required")

    entry = settings.api_key_role_map.get(api_key)
    if entry is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return Actor(
        actor_id=entry["actor_id"],
        roles=entry.get("roles", []),
    )
