"""Настройки приложения. Полный список переменных с пояснениями — в .env.example."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки из переменных окружения / .env. Агент всегда работает с реальной LLM."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    # Каталог данных и доступ к API.
    data_dir: Path = Path("data")
    app_token: SecretStr = SecretStr("")
    app_users_json: SecretStr = SecretStr("")
    # OpenAI или совместимый провайдер (OPENAI_BASE_URL).
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    model_fast: str = "gpt-4.1-mini"
    model_reasoning: str = "gpt-4.1"
    model_fallback: str = ""
    # dynamic: длинные диалоги переключаются на model_reasoning; static: всегда model_fast.
    model_strategy: Literal["static", "dynamic"] = "dynamic"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=1, le=8192)
    # Пусто — встроенный Qdrant в DATA_DIR/qdrant (один процесс).
    qdrant_url: str = ""
    qdrant_api_key: SecretStr = SecretStr("")
    # Необязательные внешние инструменты: без ключа инструмент просто не предлагается модели.
    tavily_api_key: SecretStr = SecretStr("")
    e2b_api_key: SecretStr = SecretStr("")
    # Трекинг в Langfuse: включается, когда заданы оба ключа.
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = "https://cloud.langfuse.com"
    # Бюджет одной задачи (run).
    max_model_calls: int = Field(default=16, ge=1, le=100)
    max_tool_calls: int = Field(default=30, ge=1, le=100)
    max_total_tokens: int = Field(default=48000, ge=1000)
    run_timeout_seconds: int = Field(default=300, ge=5, le=1800)

    def prepare(self):
        """Проверить обязательные настройки и создать каталоги данных (вызывается при старте)."""
        if not self.openai_api_key.get_secret_value():
            raise ValueError("Задайте OPENAI_API_KEY (или ключ совместимого провайдера) в .env.")
        self.data_dir = self.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artifacts").mkdir(exist_ok=True)
