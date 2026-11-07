from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadCreate(StrictModel):
    title: str = Field(default="Новая беседа", min_length=1, max_length=150)


class RunCreate(StrictModel):
    message: str = Field(min_length=1, max_length=24000)


class Decision(StrictModel):
    decision: Literal["approve", "edit", "reject"]
    action_hash: str
    edited_args: dict | None = None


class MemoryInput(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(default=0, ge=0)


class Answer(StrictModel):
    answer: str
    status: Literal["completed", "partial", "failed", "cancelled"] = "completed"
    citations: list[dict] = Field(default_factory=list)
    artifacts: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    support_case: dict | None = None
