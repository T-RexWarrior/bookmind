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
    with binding_lock, closing(sqlite3.connect(root / 'bookmind.sqlite', timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS textbooks (
            id TEXT PRIMARY KEY, material_id TEXT NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL UNIQUE, path_id TEXT NOT NULL UNIQUE,
            notebook_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            ranges_json TEXT NOT NULL, created_at REAL NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS generated_sources (
            native_book_id TEXT PRIMARY KEY, material_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL, goal TEXT NOT NULL, created_at REAL NOT NULL)''')
        if 'removed_at' not in {row[1] for row in conn.execute('PRAGMA table_info(textbooks)')}:
            conn.execute('ALTER TABLE textbooks ADD COLUMN removed_at REAL')
        yield conn


def books(*, include_removed=False):
    with connection() as conn:
        where = '' if include_removed else ' WHERE removed_at IS NULL'
        return [dict(row) for row in conn.execute(f'SELECT * FROM textbooks{where} ORDER BY created_at DESC')]


def find_book(*, include_removed=False, **identity):
    allowed = {'id', 'material_id', 'workspace_id', 'path_id', 'notebook_id'}
    if len(identity) != 1 or not set(identity) <= allowed:
        raise ValueError('Use one textbook identity')
    key, value = next(iter(identity.items()))
    with connection() as conn:
        where = '' if include_removed else ' AND removed_at IS NULL'
        row = conn.execute(f'SELECT * FROM textbooks WHERE {key}=?{where}', (value,)).fetchone()
        return dict(row) if row else None


def insert_book(values):
    with connection() as conn:
        conn.execute('INSERT INTO textbooks (id, material_id, workspace_id, path_id, notebook_id, title, ranges_json, created_at) VALUES(?,?,?,?,?,?,?,?)', values)


def set_removed(book_id, removed_at):
    with binding_lock, connection() as conn:
        return bool(conn.execute('UPDATE textbooks SET removed_at=? WHERE id=?', (removed_at, book_id)).rowcount)


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
