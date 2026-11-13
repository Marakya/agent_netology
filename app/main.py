"""Точка входа: сборка сервисов и приложения FastAPI.

Запуск: `uvicorn app.main:app`. Маршруты API лежат в пакете app/api.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import ROUTERS
from .config import Settings
from .db import Database
from .engine import Engine
from .providers import ArtifactStore, SandboxManager
from .rag import KnowledgeBase
from .tools import Toolbox

STATIC_DIR = Path(__file__).parent / "static"


class Services:
    """Все долгоживущие объекты приложения. Создаются один раз при старте, в порядке зависимостей."""

    def __init__(self, settings):
        self.settings = settings
        settings.prepare()
        self.db = Database(settings.data_dir / "app.sqlite")
        self.rag = KnowledgeBase(settings, self.db)
        self.artifacts = ArtifactStore(settings, self.db)
        self.sandbox = SandboxManager(settings, self.db, self.artifacts)
        self.tools = Toolbox(settings, self.db, self.rag, self.sandbox, self.artifacts)
        self.engine = Engine(settings, self.db, self.tools)

    def close(self):
        self.engine.close()
        self.rag.close()
        self.db.close()


def create_app(settings=None):
    """Создать приложение. settings передаются в тестах; по умолчанию читаются из .env."""

    @asynccontextmanager
    async def lifespan(app):
        services = Services(settings or Settings())
        app.state.services = services
        services.engine.recover()  # вернуть в работу задачи, прерванные прошлой остановкой
        yield
        services.close()

    app = FastAPI(title="Agent Netology", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    for router in ROUTERS:
        app.include_router(router)
    return app


app = create_app()
