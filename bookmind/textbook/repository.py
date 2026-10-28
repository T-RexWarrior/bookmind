"""SQLite bindings between native reading, learning, notebook and Books stores.

The existing database path and primary keys are intentionally stable. Learning
evidence and scores stay in LearningStore; this store never duplicates them.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import sqlite3
import threading

from bookmind.reading import ReadingStore

binding_lock = threading.RLock()


@contextmanager
def connection():
    root = ReadingStore().root
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'bookmind.sqlite', timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS textbooks (
            id TEXT PRIMARY KEY, material_id TEXT NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL UNIQUE, path_id TEXT NOT NULL UNIQUE,
            notebook_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            ranges_json TEXT NOT NULL, created_at REAL NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS generated_sources (
            native_book_id TEXT PRIMARY KEY, material_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL, goal TEXT NOT NULL, created_at REAL NOT NULL)''')
        yield conn


def books():
    with connection() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM textbooks ORDER BY created_at DESC')]


def find_book(**identity):
    allowed = {'id', 'material_id', 'workspace_id', 'path_id', 'notebook_id'}
    if len(identity) != 1 or not set(identity) <= allowed:
        raise ValueError('Use one textbook identity')
    key, value = next(iter(identity.items()))
    with connection() as conn:
        row = conn.execute(f'SELECT * FROM textbooks WHERE {key}=?', (value,)).fetchone()
        return dict(row) if row else None


def insert_book(values):
    with connection() as conn:
        conn.execute('INSERT INTO textbooks VALUES(?,?,?,?,?,?,?,?)', values)


def generated_source(*, material_id=None, native_book_id=None):
    if (material_id is None) == (native_book_id is None):
        raise ValueError('Use one generated source identity')
    key, value = ('material_id', material_id) if material_id is not None else ('native_book_id', native_book_id)
    with connection() as conn:
        row = conn.execute(f'SELECT * FROM generated_sources WHERE {key}=?', (value,)).fetchone()
        return dict(row) if row else None


def insert_generated_source(values):
    with connection() as conn:
        conn.execute('INSERT INTO generated_sources VALUES(?,?,?,?,?)', values)
