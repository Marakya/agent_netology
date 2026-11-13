"""Pydantic-модели API: тела запросов и итоговый ответ задачи."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Базовая модель: неизвестные поля в запросе — ошибка, а не молчаливый пропуск."""

    model_config = ConfigDict(extra="forbid")


class ThreadCreate(StrictModel):
    title: str = Field(default="Новая беседа", min_length=1, max_length=150)


class RunCreate(StrictModel):
    message: str = Field(min_length=1, max_length=24000)


class Decision(StrictModel):
    """Решение человека по подтверждению. action_hash привязывает решение к показанным аргументам."""

    decision: Literal["approve", "edit", "reject"]
    action_hash: str
    edited_args: dict | None = None


class MemoryInput(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(default=0, ge=0)


class Answer(StrictModel):
    """Итог задачи: хранится в runs.result и отдаётся в GET /api/runs/{id}."""

    answer: str
    status: Literal["completed", "partial", "failed", "cancelled"] = "completed"
    citations: list[dict] = Field(default_factory=list)  # источники, на которые ссылается ответ
    artifacts: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)  # ошибки и ограничения для пользователя
    support_case: dict | None = None
