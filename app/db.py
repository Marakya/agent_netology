import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .action_log import ActionLog


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT, created REAL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(id) ON DELETE CASCADE,
 role TEXT, content TEXT, created REAL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(id) ON DELETE CASCADE,
 owner TEXT, message TEXT, status TEXT, request_key TEXT, request_hash TEXT, created REAL,
 model_calls INTEGER DEFAULT 0, tool_calls INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0,
 result TEXT, error TEXT, cancel INTEGER DEFAULT 0, resume TEXT, UNIQUE(owner,request_key));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(thread_id)
 WHERE status IN ('queued','running','waiting_approval');
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
 type TEXT, payload TEXT, created REAL);
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, owner TEXT, key TEXT, value TEXT,
 source TEXT, updated REAL, UNIQUE(owner,key));
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, owner TEXT, name TEXT, hash TEXT,
 state TEXT, chunks INTEGER, created REAL, UNIQUE(owner,hash));
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
 owner TEXT, text TEXT, locator TEXT);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
 owner TEXT, tool TEXT, args TEXT, hash TEXT, status TEXT, decision TEXT, created REAL);
CREATE TABLE IF NOT EXISTS effects(id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
 name TEXT, status TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, owner TEXT, run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
 name TEXT, path TEXT, mime TEXT, size INTEGER, sha256 TEXT, created REAL);
CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, owner TEXT, title TEXT, body TEXT, created REAL);
"""


class Database:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}
        for name in ("active_seconds", "started_at"):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {name} REAL DEFAULT 0")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tool_usage(
          run_id TEXT REFERENCES runs(id) ON DELETE CASCADE, name TEXT, calls INTEGER,
          PRIMARY KEY(run_id,name))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS document_inputs(
          document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
          artifact_id TEXT REFERENCES artifacts(id) ON DELETE CASCADE, PRIMARY KEY(document_id,artifact_id))""")
        memory_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(memories)")}
        if "version" not in memory_columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS memory_revisions(
          memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE, version INTEGER,
          owner TEXT, key TEXT, value TEXT, source TEXT, updated REAL, mutation_id TEXT UNIQUE,
          PRIMARY KEY(memory_id,version))""")
        self.conn.execute("""INSERT OR IGNORE INTO memory_revisions
          SELECT id,version,owner,key,value,source,updated,NULL FROM memories""")
        self.conn.commit()
        self.action_log = ActionLog(Path(path).parent / "logs")

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def execute(self, sql, args=()):
        with self.transaction() as conn:
            return conn.execute(sql, args).rowcount

    def one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def event(self, run_id, event_type, payload):
        created = time.time()
        with self.transaction() as conn:
            event_id = conn.execute(
                "INSERT INTO events(run_id,type,payload,created) VALUES(?,?,?,?)",
                (run_id, event_type, dumps(payload), created),
            ).lastrowid
        self.action_log.write(run_id, event_type, payload, created, event_id)

    def message(self, thread_id, role, content, message_id=None):
        self.execute(
            "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)",
            (message_id or uid(), thread_id, role, content, time.time()),
        )

    def reserve(self, run_id, kind, limit, tokens=0, token_limit=48000):
        column = {"model": "model_calls", "tool": "tool_calls"}[kind]
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["cancel"]:
                raise InterruptedError("Задача отменена")
            if row[column] >= limit or row["tokens"] + tokens > token_limit:
                raise BudgetExceeded("Исчерпан общий бюджет задачи")
            conn.execute(f"UPDATE runs SET {column}={column}+1,tokens=tokens+? WHERE id=?", (tokens, run_id))

    def memory(self, owner):
        return self.all("SELECT * FROM memories WHERE owner=? ORDER BY updated DESC LIMIT 50", (owner,))

    def reserve_tool(self, run_id, name, total_limit):
        limits = {
            "web_search": 4,
            "web_fetch": 6,
            "run_python": 5,
            "execute": 5,
            "task": 3,
            "deep_analyze": 1,
        }
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["cancel"]:
                raise InterruptedError("Задача отменена")
            usage = conn.execute(
                "SELECT calls FROM tool_usage WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
            if row["tool_calls"] >= total_limit or (usage and usage[0] >= limits.get(name, total_limit)):
                raise BudgetExceeded("Исчерпан бюджет инструмента: " + name)
            conn.execute("UPDATE runs SET tool_calls=tool_calls+1 WHERE id=?", (run_id,))
            conn.execute(
                "INSERT INTO tool_usage VALUES(?,?,1) ON CONFLICT(run_id,name) DO UPDATE SET calls=calls+1",
                (run_id, name),
            )

    def save_memory(self, owner, key, value, source, expected_version=0, mutation_id=None):
        with self.transaction() as conn:
            if mutation_id:
                previous = conn.execute(
                    "SELECT * FROM memory_revisions WHERE mutation_id=?", (mutation_id,)
                ).fetchone()
                if previous:
                    if (previous["owner"], previous["key"], previous["value"]) != (owner, key, value):
                        raise MemoryConflict("Повтор операции содержит другое значение")
                    return dict(previous)
            current = conn.execute("SELECT * FROM memories WHERE owner=? AND key=?", (owner, key)).fetchone()
            actual_version = current["version"] if current else 0
            if actual_version != expected_version:
                raise MemoryConflict("Память уже изменилась. Обновите запись и подтвердите новое значение.")
            memory_id = current["id"] if current else uid()
            version, updated = actual_version + 1, time.time()
            conn.execute(
                """INSERT INTO memories(id,owner,key,value,source,updated,version) VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value,source=excluded.source,
              updated=excluded.updated,version=excluded.version""",
                (memory_id, owner, key, value, source, updated, version),
            )
            conn.execute(
                "INSERT INTO memory_revisions VALUES(?,?,?,?,?,?,?,?)",
                (memory_id, version, owner, key, value, source, updated, mutation_id),
            )
            return {"id": memory_id, "version": version}

    def close(self):
        with self.lock:
            self.conn.close()
            self.action_log.close()


class BudgetExceeded(RuntimeError):
    pass


class MemoryConflict(ValueError):
    pass
