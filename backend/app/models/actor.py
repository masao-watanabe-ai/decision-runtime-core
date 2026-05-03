from __future__ import annotations

from pydantic import BaseModel, Field


class Actor(BaseModel):
    """An authenticated actor interacting with the runtime."""

    actor_id: str = Field(..., min_length=1)
    roles: list[str] = Field(default_factory=list)
    actor_type: str = Field("human")
