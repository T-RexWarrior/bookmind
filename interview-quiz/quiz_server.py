"""Local persistence server for the BookMind interview quiz.

- Serves the study package directory (including 答题闯关.html).
- Stores the full quiz state in SQLite (quiz_data.sqlite).
- Keeps a bounded snapshot history so accidental writes can be inspected.
- Exposes a JSON export endpoint.

Only binds to 127.0.0.1. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import webbrowser
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "quiz_data.sqlite"
PID_PATH = ROOT / "quiz_server.pid"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOTS = 200
DEFAULT_PORT = 8021


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def load_state() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload, updated_at, revision FROM quiz_state WHERE id = 1"
        ).fetchone()
    if row is None:
        return None
    try:
        state = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    return {
        "state": state,
        "updated_at": row["updated_at"],
        "revision": row["revision"],
    }


def save_state(state: dict) -> dict:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("state payload is too large")
    stamp = now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM quiz_state WHERE id = 1").fetchone()
        revision = (row["revision"] if row else 0) + 1
        conn.execute(
            """
            INSERT INTO quiz_state (id, payload, updated_at, revision)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at,
                revision = excluded.revision
            """,
            (payload, stamp, revision),
        )
        conn.execute(
            "INSERT INTO quiz_snapshots (payload, created_at) VALUES (?, ?)",
            (payload, stamp),
        )
        conn.execute(
            """
            DELETE FROM quiz_snapshots
            WHERE id NOT IN (
                SELECT id FROM quiz_snapshots ORDER BY id DESC LIMIT ?
            )
            """,
            (MAX_SNAPSHOTS,),
        )
        conn.commit()
    return {"revision": revision, "updated_at": stamp}


class QuizHandler(SimpleHTTPRequestHandler):
    server_version = "BookMindQuiz/1.0"

    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):  # noqa: ANN001
        print(f"[quiz] {self.address_string()} {fmt % args}")

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        # Allow the local page and file:// pages (Origin: null) to call the API.
        if origin in {"null", "http://127.0.0.1:8021", "http://localhost:8021"}:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def end_headers(self) -> None:
        self._cors()
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("invalid request body size")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("body must be valid UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm", ".py", ".pid", ".log")):
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/%E7%AD%94%E9%A2%98%E9%97%AF%E5%85%B3.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/health":
            current = load_state()
            self._send_json(
                {
                    "ok": True,
                    "storage": "sqlite",
                    "db": str(DB_PATH),
                    "has_state": current is not None,
                    "revision": current["revision"] if current else 0,
                }
            )
            return
        if path == "/api/state":
            self._send_json(load_state() or {"state": None, "revision": 0})
            return
        if path == "/api/export":
            current = load_state()
            payload = {
                "app": "bookmind-interview-quiz",
                "schema": 1,
                "exported_at": now_iso(),
                "state": current["state"] if current else None,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="bookmind-quiz-backup.json"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in {"/api/state", "/api/import"}:
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        try:
            body = self._read_json()
            state = body.get("state", body)
            if not isinstance(state, dict):
                raise ValueError("state must be a JSON object")
            saved = save_state(state)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"ok": False, "error": f"save failed: {exc}"}, status=500)
            return
        self._send_json({"ok": True, **saved})


def main() -> None:
    parser = argparse.ArgumentParser(description="BookMind interview quiz local server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="open the quiz in a browser")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    init_db()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"BookMind quiz server: {url}")
    print(f"SQLite file: {DB_PATH}")
    print("Press Ctrl+C to stop.")

    if args.open and not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), QuizHandler)
    except OSError as exc:
        print(f"Port {args.port} is already in use: {exc}")
        print("The quiz server may already be running; open", url)
        if args.open and not args.no_browser:
            webbrowser.open(url)
        return

    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        PID_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
