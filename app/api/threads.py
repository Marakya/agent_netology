"""Беседы и запуск задач."""

import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException

from ..db import digest, uid
from ..models import RunCreate, ThreadCreate
from .deps import assert_idle, owned, principal, services

router = APIRouter(prefix="/api/threads")


@router.get("")
def threads(owner=Depends(principal), s=Depends(services)):
    return s.db.all("SELECT * FROM threads WHERE owner=? ORDER BY created DESC", (owner,))


@router.post("", status_code=201)
def create_thread(body: ThreadCreate, owner=Depends(principal), s=Depends(services)):
    thread_id = uid()
    s.db.execute("INSERT INTO threads VALUES(?,?,?,?)", (thread_id, owner, body.title, time.time()))
    return {"id": thread_id, "title": body.title}


@router.get("/{thread_id}")
def thread(thread_id: str, owner=Depends(principal), s=Depends(services)):
    """Беседа с видимой историей сообщений и списком задач."""
    row = owned(s, "threads", thread_id, owner)
    row["messages"] = s.db.all(
        "SELECT role,content,created FROM messages WHERE thread_id=? ORDER BY created", (thread_id,)
    )
    row["runs"] = s.db.all(
        "SELECT id,status,model_calls,tool_calls,tokens,result FROM runs WHERE thread_id=? ORDER BY created",
        (thread_id,),
    )
    return row


@router.post("/{thread_id}/runs", status_code=202)
def run(
    thread_id: str,
    body: RunCreate,
    owner=Depends(principal),
    s=Depends(services),
    idempotency_key: str = Header(min_length=1, max_length=200),
):
    """Создать задачу по сообщению пользователя и поставить её в очередь.

    Заголовок Idempotency-Key защищает от дублей при повторе запроса: тот же ключ с тем же
    сообщением возвращает уже созданную задачу, с другим сообщением — 409.
    """
    owned(s, "threads", thread_id, owner)
    request_hash = digest([thread_id, body.message])
    existing = s.db.one("SELECT * FROM runs WHERE owner=? AND request_key=?", (owner, idempotency_key))
    if existing:
        if existing["request_hash"] != request_hash:
            raise HTTPException(409, "Idempotency-Key уже использован для другого запроса")
        return {"id": existing["id"], "status": existing["status"]}
    run_id = uid()
    try:
        # Задача и сообщение пользователя сохраняются одной транзакцией.
        with s.db.transaction() as conn:
            conn.execute(
                """INSERT INTO runs(id,thread_id,owner,message,status,request_key,request_hash,created)
                  VALUES(?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    thread_id,
                    owner,
                    body.message,
                    "queued",
                    idempotency_key,
                    request_hash,
                    time.time(),
                ),
            )
            conn.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?)",
                (run_id, thread_id, "user", body.message, time.time()),
            )
    except sqlite3.IntegrityError:
        # Сработал уникальный индекс one_active_run: в беседе уже есть активная задача.
        raise HTTPException(409, "В беседе уже выполняется задача") from None
    s.db.event(run_id, "run.queued", {"model": s.settings.model_fast})
    s.engine.submit(run_id)
    return {"id": run_id, "status": "queued"}


@router.delete("/{thread_id}")
def delete_thread(thread_id: str, owner=Depends(principal), s=Depends(services)):
    """Удалить беседу: файлы её задач, checkpoint и все связанные строки (каскадно)."""
    assert_idle(s, owner)
    owned(s, "threads", thread_id, owner)
    for row in s.db.all(
        "SELECT a.path FROM artifacts a JOIN runs r ON a.run_id=r.id WHERE r.thread_id=?", (thread_id,)
    ):
        Path(row["path"]).unlink(missing_ok=True)
    s.engine.saver.delete_thread(thread_id)
    s.db.execute("DELETE FROM threads WHERE id=?", (thread_id,))
    return {"deleted": True}
