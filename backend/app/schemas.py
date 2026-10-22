from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(APIModel):
    status: Literal["ok"] = "ok"
    service: Literal["agentx-api"] = "agentx-api"
    database: Literal["ok"] = "ok"
    phase: Literal[1] = 1


class ErrorDetail(APIModel):
    code: str
    message: str


class ErrorResponse(APIModel):
    error: ErrorDetail



class TaskStatus(str, Enum):
    pending = 'pending'
    planning = 'planning'
    awaiting_user_input = 'awaiting_user_input'
    awaiting_approval = 'awaiting_approval'
    running = 'running'
    blocked = 'blocked'
    completed = 'completed'
    failed = 'failed'
    cancelled = 'cancelled'


class TaskCreate(APIModel):
    description: str = Field(min_length=1, max_length=5000, strict=True)
    conversation_id: UUID | None = None

    @field_validator('description')
    @classmethod
    def meaningful_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('Description must not be blank')
        return value


class ConversationUpdate(APIModel):
    title: str | None = Field(default=None, min_length=1, max_length=120, strict=True)
    archived: bool | None = Field(default=None, strict=True)

    @field_validator('title')
    @classmethod
    def clean_title(cls, value):
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError('Title must not be blank')
        return value


class TaskResponse(APIModel):
    conversation_id: UUID | None = None
    response_mode: Literal['chat'] | None = None
    title: str | None = None
    archived: bool = False
    id: UUID
    description: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    plan: dict[str, JsonValue] | None = None
    final_result: str | None = None
    error: str | None = None


class TaskListResponse(APIModel):
    tasks: list[TaskResponse]
    limit: int
    offset: int


class HistoryEvent(APIModel):
    id: int
    task_id: UUID
    event_type: Literal['task_created', 'status_changed']
    status: TaskStatus
    message: str
    created_at: datetime


class HistoryResponse(APIModel):
    events: list[HistoryEvent]
