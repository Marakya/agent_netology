"""Служебные маршруты: конфигурация для UI, реестр заявок, скачивание артефактов."""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from .deps import owned, principal, services

router = APIRouter(prefix="/api")


@router.get("/config")
def config(owner=Depends(principal), s=Depends(services)):
    """Что подключено: модель, Qdrant, embeddings, доступные инструменты. Секретов не содержит."""
    return {
        "owner": owner,
        "model": s.settings.model_fast,
        "qdrant": "server" if s.settings.qdrant_url else "embedded",
        "embedding_model": s.settings.embedding_model,
        "embedding_dimensions": s.rag.dim,
        "collection": s.rag.collection,
        "web_search": bool(s.settings.tavily_api_key.get_secret_value()),
        "sandbox": bool(s.settings.e2b_api_key.get_secret_value()),
        "tools": s.tools.available(),
    }


@router.get("/tickets")
def tickets(owner=Depends(principal), s=Depends(services)):
    """Локальный реестр заявок (создаются инструментом create_ticket после подтверждения)."""
    return s.db.all("SELECT * FROM tickets WHERE owner=? ORDER BY created DESC LIMIT 100", (owner,))


@router.get("/artifacts/{artifact_id}")
def download(artifact_id: str, owner=Depends(principal), s=Depends(services)):
    """Скачать файл. Заголовки запрещают браузеру исполнять содержимое (например, HTML-отчёт)."""
    row = owned(s, "artifacts", artifact_id, owner)
    return FileResponse(
        row["path"],
        filename=row["name"],
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )
