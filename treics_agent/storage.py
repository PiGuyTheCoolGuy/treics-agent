from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "agent.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, step INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
                max_steps INTEGER NOT NULL, model TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                step INTEGER NOT NULL, time REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
            CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL);
            INSERT OR IGNORE INTO memory VALUES (1, '');
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
        for name, definition in (("task_checklist", "INTEGER NOT NULL DEFAULT 0"),
                                 ("checklist", "TEXT NOT NULL DEFAULT '[]'")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        self.db.commit()

    def recover(self) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE runs SET status='interrupted', updated=?, "
                            "summary='Controller restarted; task was not automatically resumed.' "
                            "WHERE status IN ('running', 'paused', 'pausing', 'stopping')", (time.time(),))

    def create_run(self, task: str, max_steps: int, model: str, task_checklist: bool = False) -> str:
        run_id = uuid.uuid4().hex
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO runs(id,task,status,created,updated,max_steps,model,task_checklist) "
                            "VALUES (?,?,'running',?,?,?,?,?)", (run_id, task, now, now, max_steps, model, task_checklist))
        return run_id

    def update(self, run_id: str, **values) -> None:
        allowed = {"status", "step", "state", "summary", "checklist"}
        if values.keys() - allowed:
            raise ValueError("Unknown run fields")
        if "checklist" in values:
            values["checklist"] = json.dumps(values["checklist"], ensure_ascii=False)
        values["updated"] = time.time()
        with self.lock, self.db:
            self.db.execute("UPDATE runs SET " + ", ".join(k + "=?" for k in values) + " WHERE id=?",
                            (*values.values(), run_id))

    def run(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return self.decode_run(row) if row else None

    @staticmethod
    def decode_run(row) -> dict:
        run = dict(row)
        run["task_checklist"] = bool(run["task_checklist"])
        run["checklist"] = json.loads(run["checklist"])
        return run

    def runs(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [self.decode_run(r) for r in self.db.execute("SELECT * FROM runs ORDER BY created DESC LIMIT ?", (limit,))]

    def event(self, run_id: str, step: int, kind: str, data: dict) -> int:
        with self.lock, self.db:
            cursor = self.db.execute("INSERT INTO events(run_id,step,time,kind,data) VALUES (?,?,?,?,?)",
                                     (run_id, step, time.time(), kind, json.dumps(data, ensure_ascii=False)))
            return cursor.lastrowid

    def evidence(self, run_id: str, event_id: int) -> dict | None:
        if type(event_id) is not int or not 1 <= event_id <= 9223372036854775807:
            return None
        with self.lock:
            row = self.db.execute("SELECT * FROM events WHERE run_id=? AND id=? "
                                  "AND kind IN ('action','error')", (run_id, event_id)).fetchone()
        if not row:
            return None
        event = {**dict(row), "data": json.loads(row["data"])}
        return None if event["data"].get("action_executed") is False else event

    def events_after(self, run_id: str, event_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE run_id=? AND id>? "
                                   "AND kind IN ('action','error') ORDER BY id LIMIT 101", (run_id, event_id)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def events(self, run_id: str, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                                   (run_id, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in reversed(rows)]

    def memory(self) -> str:
        with self.lock:
            return self.db.execute("SELECT text FROM memory WHERE id=1").fetchone()[0]

    def remember(self, text: str) -> None:
        if not isinstance(text, str) or len(text) > 6000:
            raise ValueError("Persistent notes must be text of at most 6000 characters")
        with self.lock, self.db:
            self.db.execute("UPDATE memory SET text=? WHERE id=1", (text,))

    def close(self) -> None:
        with self.lock:
            self.db.close()
