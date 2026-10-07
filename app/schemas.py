from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class EventCreate(BaseModel):
    type: str
    payload: Any
    webhook_url: str


class AttemptResponse(BaseModel):
    attempted_at: datetime
    http_status: int | None
    outcome: Literal["success", "failed"]


class EventResponse(BaseModel):
    id: int
    type: str
    payload: Any
    webhook_url: str
    status: Literal["pending", "delivered", "failed", "dead"]
    created_at: datetime
    attempts: list[AttemptResponse]
