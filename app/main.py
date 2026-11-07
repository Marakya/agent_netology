import asyncio
import hmac
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database, MemoryConflict, digest, uid
from .engine import Engine
from .models import Decision, MemoryInput, RunCreate, ThreadCreate
from .providers import ArtifactStore, SandboxManager
from .rag import KnowledgeBase
from .security import SECRET
from .tools import Toolbox


class Services:
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
    @asynccontextmanager
    async def lifespan(app):
        services = Services(settings or Settings())
        app.state.services = services
        services.engine.recover()
        yield
        services.close()

    app = FastAPI(title="Agent Netology", version="0.1.0", lifespan=lifespan)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    def services(request: Request):
        return request.app.state.services

    def principal(request: Request, s=Depends(services), authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ")
        raw_users = s.settings.app_users_json.get_secret_value()
        if raw_users:
            for expected, owner in json.loads(raw_users).items():
                if hmac.compare_digest(token, expected):
                    return str(owner)
            raise HTTPException(401, "Нужен действующий токен доступа")
        expected = s.settings.app_token.get_secret_value()
        if expected:
            if hmac.compare_digest(token, expected):
                return "local-user"
            raise HTTPException(401, "Нужен токен APP_TOKEN")
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1", "testserver"} or request.client.host not in {
            "127.0.0.1",
            "::1",
            "testclient",
        }:
            raise HTTPException(403, "Без токена доступ разрешён только с localhost")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin запрос запрещён")
        return "local-user"

    def owned(s, table, object_id, owner):
        if table not in {"threads", "runs", "approvals", "artifacts", "memories"}:
            raise ValueError("Unknown table")
        row = s.db.one(f"SELECT * FROM {table} WHERE id=? AND owner=?", (object_id, owner))
        if not row:
            raise HTTPException(404, "Объект не найден")
        return row

    def assert_idle(s, owner):
        if s.db.one(
            "SELECT id FROM runs WHERE owner=? AND status IN ('queued','running','waiting_approval')",
            (owner,),
        ):
            raise HTTPException(409, "Сначала завершите или отмените активную задачу")
        with s.engine.lock:
            for run_id in s.engine.active:
                if s.db.one("SELECT id FROM runs WHERE id=? AND owner=?", (run_id, owner)):
                    raise HTTPException(409, "Задача завершает сохранение. Повторите через секунду")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config(owner=Depends(principal), s=Depends(services)):
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

    @app.get("/api/threads")
    def threads(owner=Depends(principal), s=Depends(services)):
        return s.db.all("SELECT * FROM threads WHERE owner=? ORDER BY created DESC", (owner,))

    @app.post("/api/threads", status_code=201)
    def create_thread(body: ThreadCreate, owner=Depends(principal), s=Depends(services)):
        thread_id = uid()
        s.db.execute("INSERT INTO threads VALUES(?,?,?,?)", (thread_id, owner, body.title, time.time()))
        return {"id": thread_id, "title": body.title}

    @app.get("/api/threads/{thread_id}")
    def thread(thread_id: str, owner=Depends(principal), s=Depends(services)):
        row = owned(s, "threads", thread_id, owner)
        row["messages"] = s.db.all(
            "SELECT role,content,created FROM messages WHERE thread_id=? ORDER BY created", (thread_id,)
        )
        row["runs"] = s.db.all(
            "SELECT id,status,model_calls,tool_calls,tokens,result FROM runs WHERE thread_id=? ORDER BY created",
            (thread_id,),
        )
        return row

    @app.post("/api/threads/{thread_id}/runs", status_code=202)
    def run(
        thread_id: str,
        body: RunCreate,
        owner=Depends(principal),
        s=Depends(services),
        idempotency_key: str = Header(min_length=1, max_length=200),
    ):
        owned(s, "threads", thread_id, owner)
        request_hash = digest([thread_id, body.message])
        existing = s.db.one("SELECT * FROM runs WHERE owner=? AND request_key=?", (owner, idempotency_key))
        if existing:
            if existing["request_hash"] != request_hash:
                raise HTTPException(409, "Idempotency-Key уже использован для другого запроса")
            return {"id": existing["id"], "status": existing["status"]}
        run_id = uid()
        try:
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
            raise HTTPException(409, "В беседе уже выполняется задача") from None
        s.db.event(run_id, "run.queued", {"model": s.settings.model_fast})
        s.engine.submit(run_id)
        return {"id": run_id, "status": "queued"}

    @app.get("/api/runs/{run_id}/log")
    def action_log(
        run_id: str,
        limit: int = Query(default=200, ge=1, le=500),
        owner=Depends(principal),
        s=Depends(services),
    ):
        from .action_log import entry

        owned(s, "runs", run_id, owner)
        rows = s.db.all("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit))
        total = s.db.one("SELECT COUNT(*) AS n FROM events WHERE run_id=?", (run_id,))["n"]
        return {
            "events": [
                entry(run_id, r["type"], json.loads(r["payload"]), r["created"], r["id"])
                for r in reversed(rows)
            ],
            "total": total,
            "truncated": total > limit,
        }

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str, owner=Depends(principal), s=Depends(services)):
        row = owned(s, "runs", run_id, owner)
        row["result"] = json.loads(row["result"]) if row["result"] else None
        row["approvals"] = s.db.all(
            "SELECT id,tool,args,hash,status FROM approvals WHERE run_id=? AND status='pending'", (run_id,)
        )
        for a in row["approvals"]:
            a["args"] = json.loads(a["args"])
        return row

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str, request: Request, after: int = 0, owner=Depends(principal), s=Depends(services)
    ):
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
                    # Completion can commit after the first SELECT but before status is read.
                    if s.db.one("SELECT id FROM events WHERE run_id=? AND id>? LIMIT 1", (run_id, cursor)):
                        continue
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.3)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, owner=Depends(principal), s=Depends(services)):
        owned(s, "runs", run_id, owner)
        with s.db.transaction() as conn:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            pending = row and row["status"] in {"queued", "running", "waiting_approval"}
            waiting = pending and row["status"] == "waiting_approval"
            if pending:
                conn.execute("UPDATE runs SET cancel=1 WHERE id=?", (run_id,))
        if waiting:
            from .models import Answer

            s.engine.complete(
                run_id,
                Answer(answer="Задача отменена", status="cancelled", limitations=["Отмена пользователем"]),
            )
        if pending:
            s.db.event(run_id, "run.cancel_requested", {})
        return {"cancel_requested": True}

    @app.post("/api/approvals/{approval_id}")
    def decision(approval_id: str, body: Decision, owner=Depends(principal), s=Depends(services)):
        row = owned(s, "approvals", approval_id, owner)
        if row["hash"] != body.action_hash or time.time() - row["created"] > 86400:
            raise HTTPException(409, "Подтверждение устарело")
        if body.decision == "edit":
            if body.edited_args is None:
                raise HTTPException(422, "Нужны edited_args")
            try:
                if row["tool"] == "save_memory" and body.edited_args.get("expected_version") is None:
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
            conn.execute(
                "UPDATE approvals SET status='decided',decision=? WHERE id=?", (serialized, approval_id)
            )
            conn.execute("UPDATE runs SET status='queued',resume=? WHERE id=?", (serialized, row["run_id"]))
        s.db.event(row["run_id"], "approval.decided", {"name": row["tool"], "decision": body.decision})
        s.engine.submit(row["run_id"])
        return {"id": row["run_id"], "status": "queued"}

    @app.get("/api/memory")
    def memory(owner=Depends(principal), s=Depends(services)):
        return s.db.memory(owner)

    @app.post("/api/memory")
    def memory_save(body: MemoryInput, owner=Depends(principal), s=Depends(services)):
        if SECRET.search(body.value):
            raise HTTPException(422, "Секреты нельзя сохранять в память")
        try:
            saved = s.db.save_memory(owner, body.key, body.value, "user:explicit-save", body.expected_version)
        except MemoryConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return {"saved": True, **saved}

    @app.get("/api/memory/{memory_id}/history")
    def memory_history(memory_id: str, owner=Depends(principal), s=Depends(services)):
        owned(s, "memories", memory_id, owner)
        return s.db.all(
            "SELECT version,key,value,source,updated FROM memory_revisions WHERE memory_id=? AND owner=? ORDER BY version DESC",
            (memory_id, owner),
        )

    @app.delete("/api/memory/{memory_id}")
    def memory_delete(memory_id: str, owner=Depends(principal), s=Depends(services)):
        assert_idle(s, owner)
        owned(s, "memories", memory_id, owner)
        s.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        # Clear model checkpoints so an old tool result cannot silently restore a forgotten preference.
        for t in s.db.all("SELECT id FROM threads WHERE owner=?", (owner,)):
            s.engine.saver.delete_thread(t["id"])
        return {
            "deleted": True,
            "context_reset": True,
            "note": "История сообщений остаётся видимой; для полного удаления удалите беседу.",
        }

    @app.get("/api/documents")
    def documents(owner=Depends(principal), s=Depends(services)):
        return s.db.all("SELECT * FROM documents WHERE owner=? ORDER BY created DESC", (owner,))

    @app.post("/api/support/sample")
    def load_sample(owner=Depends(principal), s=Depends(services)):
        from .support import SAMPLE_DIR

        assert_idle(s, owner)
        documents = []
        with s.rag.lock:
            for path in sorted(SAMPLE_DIR.glob("*.md")):
                data = path.read_bytes()
                doc = s.rag.ingest(owner, "Лист — " + path.name, data)
                if not s.db.one("SELECT artifact_id FROM document_inputs WHERE document_id=?", (doc["id"],)):
                    artifact = s.artifacts.save(owner, None, path.name, data)
                    s.db.execute(
                        "INSERT INTO document_inputs VALUES(?,?)", (doc["id"], artifact["artifact_id"])
                    )
                documents.append(doc)
        return {"store": "Лист", "synthetic": True, "documents": documents}

    @app.get("/api/tickets")
    def tickets(owner=Depends(principal), s=Depends(services)):
        return s.db.all("SELECT * FROM tickets WHERE owner=? ORDER BY created DESC LIMIT 100", (owner,))

    @app.post("/api/documents", status_code=201)
    def upload(file: UploadFile = File(), owner=Depends(principal), s=Depends(services)):
        data = file.file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "Максимальный файл 10 MiB")
        try:
            doc = s.rag.ingest(owner, file.filename or "document.txt", data)
            existing = s.db.one("SELECT artifact_id FROM document_inputs WHERE document_id=?", (doc["id"],))
            if not existing:
                artifact = s.artifacts.save(owner, None, file.filename or "document.txt", data)
                s.db.execute("INSERT INTO document_inputs VALUES(?,?)", (doc["id"], artifact["artifact_id"]))
            return doc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete("/api/documents/{doc_id}")
    def document_delete(doc_id: str, owner=Depends(principal), s=Depends(services)):
        assert_idle(s, owner)
        inputs = s.db.all(
            """SELECT a.* FROM artifacts a JOIN document_inputs d ON a.id=d.artifact_id
                            WHERE d.document_id=? AND a.owner=?""",
            (doc_id, owner),
        )
        try:
            s.rag.delete(owner, doc_id)
        except KeyError:
            raise HTTPException(404, "Документ не найден") from None
        for artifact in inputs:
            Path(artifact["path"]).unlink(missing_ok=True)
            s.db.execute("DELETE FROM artifacts WHERE id=?", (artifact["id"],))
        for t in s.db.all("SELECT id FROM threads WHERE owner=?", (owner,)):
            s.engine.saver.delete_thread(t["id"])
        return {"deleted_from_index": True, "context_reset": True}

    @app.get("/api/artifacts/{artifact_id}")
    def download(artifact_id: str, owner=Depends(principal), s=Depends(services)):
        row = owned(s, "artifacts", artifact_id, owner)
        return FileResponse(
            row["path"],
            filename=row["name"],
            media_type="application/octet-stream",
            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
        )

    @app.delete("/api/threads/{thread_id}")
    def delete_thread(thread_id: str, owner=Depends(principal), s=Depends(services)):
        assert_idle(s, owner)
        owned(s, "threads", thread_id, owner)
        for row in s.db.all(
            "SELECT a.path FROM artifacts a JOIN runs r ON a.run_id=r.id WHERE r.thread_id=?", (thread_id,)
        ):
            Path(row["path"]).unlink(missing_ok=True)
        s.engine.saver.delete_thread(thread_id)
        s.db.execute("DELETE FROM threads WHERE id=?", (thread_id,))
        return {"deleted": True}

    return app


app = create_app()
