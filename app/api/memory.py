"""Подтверждённая память владельца: факты и предпочтения, общие для всех его бесед."""

from fastapi import APIRouter, Depends, HTTPException

from ..db import MemoryConflict
from ..models import MemoryInput
from ..security import SECRET
from .deps import assert_idle, owned, principal, reset_context, services

router = APIRouter(prefix="/api/memory")


@router.get("")
def memory(owner=Depends(principal), s=Depends(services)):
    return s.db.memory(owner)


@router.post("")
def memory_save(body: MemoryInput, owner=Depends(principal), s=Depends(services)):
    """Сохранить запись вручную. expected_version — версия, которую видел пользователь (0 — новая)."""
    if SECRET.search(body.value):
        raise HTTPException(422, "Секреты нельзя сохранять в память")
    try:
        saved = s.db.save_memory(owner, body.key, body.value, "user:explicit-save", body.expected_version)
    except MemoryConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {"saved": True, **saved}


@router.get("/{memory_id}/history")
def memory_history(memory_id: str, owner=Depends(principal), s=Depends(services)):
    owned(s, "memories", memory_id, owner)
    return s.db.all(
        "SELECT version,key,value,source,updated FROM memory_revisions WHERE memory_id=? AND owner=? ORDER BY version DESC",
        (memory_id, owner),
    )


@router.delete("/{memory_id}")
def memory_delete(memory_id: str, owner=Depends(principal), s=Depends(services)):
    assert_idle(s, owner)
    owned(s, "memories", memory_id, owner)
    s.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    # Иначе старый результат read_memory в истории модели «вернул бы» забытое предпочтение.
    reset_context(s, owner)
    return {
        "deleted": True,
        "context_reset": True,
        "note": "История сообщений остаётся видимой; для полного удаления удалите беседу.",
    }
