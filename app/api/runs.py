"""Задачи: состояние, журнал действий, поток событий (SSE), отмена и подтверждения."""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..action_log import entry
from ..models import Answer, Decision
from .deps import ACTIVE_STATUSES, owned, principal, services

router = APIRouter(prefix="/api")

APPROVAL_TTL_SECONDS = 86400  # предложение действия старше суток считается устаревшим
SSE_POLL_SECONDS = 0.3


@router.get("/runs/{run_id}")
def run_status(run_id: str, owner=Depends(principal), s=Depends(services)):
    """Состояние задачи: статус, счётчики бюджета, результат и ожидающие подтверждения."""
    row = owned(s, "runs", run_id, owner)
    row["result"] = json.loads(row["result"]) if row["result"] else None
    row["approvals"] = s.db.all(
        "SELECT id,tool,args,hash,status FROM approvals WHERE run_id=? AND status='pending'", (run_id,)
    )
    for approval in row["approvals"]:
        approval["args"] = json.loads(approval["args"])
    return row


@router.get("/runs/{run_id}/log")
def action_log(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    owner=Depends(principal),
    s=Depends(services),
):
    """Журнал действий агента: последние limit событий в безопасном формате (без текстов)."""
    owned(s, "runs", run_id, owner)
    rows = s.db.all("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit))
    total = s.db.one("SELECT COUNT(*) AS n FROM events WHERE run_id=?", (run_id,))["n"]
    return {
        "events": [
            entry(run_id, r["type"], json.loads(r["payload"]), r["created"], r["id"]) for r in reversed(rows)
        ],
        "total": total,
        "truncated": total > limit,
    }


@router.get("/runs/{run_id}/events")
async def events(
    run_id: str, request: Request, after: int = 0, owner=Depends(principal), s=Depends(services)
):
    """Поток событий задачи (Server-Sent Events).

    Клиент может продолжить с места обрыва: параметр after или заголовок Last-Event-ID.
    Поток закрывается, когда задача перестала выполняться и все события отданы.
    """
    owned(s, "runs", run_id, owner)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "Некорректный Last-Event-ID") from None

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            rows = s.db.all(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 100", (run_id, cursor)
            )
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\nevent: {row['type']}\ndata: {row['payload']}\n\n"
            status = s.db.one("SELECT status FROM runs WHERE id=?", (run_id,))
            if not status or status["status"] not in {"queued", "running"}:
                # Завершение могло записаться между чтением событий и чтением статуса:
                # если появились новые события, отдаём их перед закрытием.
                if s.db.one("SELECT id FROM events WHERE run_id=? AND id>? LIMIT 1", (run_id, cursor)):
                    continue
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str, owner=Depends(principal), s=Depends(services)):
    """Запросить отмену. Выполняющаяся задача остановится на ближайшей контрольной точке."""
    owned(s, "runs", run_id, owner)
    with s.db.transaction() as conn:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        pending = row and row["status"] in ACTIVE_STATUSES
        waiting = pending and row["status"] == "waiting_approval"
        if pending:
            conn.execute("UPDATE runs SET cancel=1 WHERE id=?", (run_id,))
    if waiting:
        # Задача ждёт человека и не выполняется: завершаем её сразу, без потока движка.
        s.engine.complete(
            run_id,
            Answer(answer="Задача отменена", status="cancelled", limitations=["Отмена пользователем"]),
        )
    if pending:
        s.db.event(run_id, "run.cancel_requested", {})
    return {"cancel_requested": True}


@router.post("/approvals/{approval_id}")
def decision(approval_id: str, body: Decision, owner=Depends(principal), s=Depends(services)):
    """Принять решение человека (approve / edit / reject) и продолжить задачу.

    action_hash в теле должен совпасть с hash предложения: человек подтверждает ровно те
    аргументы, которые видел. Повтор того же решения безопасен, другое решение — 409.
    """
    row = owned(s, "approvals", approval_id, owner)
    if row["hash"] != body.action_hash or time.time() - row["created"] > APPROVAL_TTL_SECONDS:
        raise HTTPException(409, "Подтверждение устарело")
    if body.decision == "edit":
        if body.edited_args is None:
            raise HTTPException(422, "Нужны edited_args")
        try:
            if row["tool"] == "save_memory" and body.edited_args.get("expected_version") is None:
                # Версию памяти человек не редактирует: для того же ключа берём её из предложения,
                # для нового ключа — текущую версию записи.
                original = json.loads(row["args"])
                if body.edited_args.get("key") == original["key"]:
                    body.edited_args["expected_version"] = original["expected_version"]
                else:
                    current = s.db.one(
                        "SELECT version FROM memories WHERE owner=? AND key=?",
                        (owner, body.edited_args.get("key")),
                    )
                    body.edited_args["expected_version"] = current["version"] if current else 0
            s.tools.validate(row["tool"], body.edited_args)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    elif body.edited_args is not None:
        raise HTTPException(422, "edited_args допустимы только для edit")
    serialized = body.model_dump_json()
    with s.db.transaction() as conn:
        fresh = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if fresh["status"] != "pending":
            if fresh["decision"] == serialized:
                return {"id": row["run_id"], "already_decided": True}
            raise HTTPException(409, "Решение уже принято")
        run = conn.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
        if run["status"] != "waiting_approval":
            raise HTTPException(409, "Задача не ожидает подтверждения")
        conn.execute("UPDATE approvals SET status='decided',decision=? WHERE id=?", (serialized, approval_id))
        # Решение кладётся в runs.resume: Engine.work передаст его графу как Command(resume=...).
        conn.execute("UPDATE runs SET status='queued',resume=? WHERE id=?", (serialized, row["run_id"]))
    s.db.event(row["run_id"], "approval.decided", {"name": row["tool"], "decision": body.decision})
    s.engine.submit(row["run_id"])
    return {"id": row["run_id"], "status": "queued"}
