from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(
    os.environ.get("BOOKMIND_DATA_DIR")
    or os.environ.get("STUDYTUTOR_DATA_DIR")
    or ROOT / "data"
).resolve()
DATA.mkdir(parents=True, exist_ok=True)
(DATA / "uploads").mkdir(exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False)


@contextmanager
def connection():
    conn = sqlite3.connect(DATA / "studytutor.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init():
    with connection() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,title TEXT NOT NULL,goal TEXT NOT NULL DEFAULT '',focus_material_id TEXT,focus_page INTEGER NOT NULL DEFAULT 1,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS materials(id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,title TEXT NOT NULL,kind TEXT NOT NULL,filename TEXT,sha TEXT NOT NULL,page_count INTEGER NOT NULL,outline TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pages(material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,number INTEGER NOT NULL,title TEXT NOT NULL,text TEXT NOT NULL,PRIMARY KEY(material_id,number));
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,role TEXT NOT NULL,content TEXT NOT NULL,refs TEXT NOT NULL,artifact_id TEXT,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,title TEXT NOT NULL,format TEXT NOT NULL,payload TEXT NOT NULL,refs TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quizzes(id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,title TEXT NOT NULL,topic TEXT NOT NULL,questions TEXT NOT NULL,refs TEXT NOT NULL,source_sha TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,quiz_id TEXT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,question_id TEXT NOT NULL,answer TEXT NOT NULL,score REAL NOT NULL,feedback TEXT NOT NULL,hint_used INTEGER NOT NULL,created TEXT NOT NULL,UNIQUE(quiz_id,question_id));
        CREATE TABLE IF NOT EXISTS hints(quiz_id TEXT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,question_id TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(quiz_id,question_id));
        CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,title TEXT NOT NULL,content TEXT NOT NULL,source TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,space_id TEXT REFERENCES spaces(id) ON DELETE CASCADE,kind TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,partial TEXT NOT NULL DEFAULT '',result TEXT,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS messages_space ON messages(space_id,created);
        CREATE INDEX IF NOT EXISTS materials_space ON materials(space_id);
        CREATE INDEX IF NOT EXISTS notes_space ON notes(space_id,updated);
        """)
        c.execute("UPDATE jobs SET status='failed',stage='服务重新启动',error='上次任务被服务重启中断，请重新发起。',updated=? WHERE status IN ('queued','running')", (now(),))


def one(sql, args=()):
    with connection() as c:
        row = c.execute(sql, args).fetchone()
        return dict(row) if row else None


def all(sql, args=()):
    with connection() as c:
        return [dict(row) for row in c.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with connection() as c:
        c.execute(sql, args)


def decode(row, *fields):
    if row:
        for f in fields:
            if row.get(f) is not None:
                row[f] = json.loads(row[f])
    return row


def material(row):
    return decode(row, "outline")


def new_material(space_id, title, kind, pages, outline, sha, filename=None):
    mid = uid()
    with connection() as c:
        c.execute("INSERT INTO materials VALUES(?,?,?,?,?,?,?,?,?)", (mid, space_id, title, kind, filename, sha, len(pages), dump(outline), now()))
        c.executemany("INSERT INTO pages VALUES(?,?,?,?)", [(mid, i + 1, p["title"], p["text"]) for i, p in enumerate(pages)])
        c.execute("UPDATE spaces SET focus_material_id=?,focus_page=1,updated=? WHERE id=?", (mid, now(), space_id))
    return material(one("SELECT * FROM materials WHERE id=?", (mid,)))


def update_job(jid, **fields):
    allowed = {"status", "stage", "partial", "result", "error"}
    values = {k: dump(v) if k == "result" else v for k, v in fields.items() if k in allowed}
    values["updated"] = now()
    execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), jid))
