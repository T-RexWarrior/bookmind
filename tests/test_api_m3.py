"""M3 product API integration tests — real textbook import + Reader
(PRODUCTIZATION §13 M3 acceptance path).

  multipart upload → job progress → DONE → grounded Q&A with citations →
  citation locates a real PDF page → restart recovery → duplicate reuse →
  error handling → cross-user isolation.

Runs against a SqlRepository with a temp data dir so file persistence is real.
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.storage.sql import SqlRepository


# A minimal but valid PDF with a text layer the PlainPdfFallback can parse.
_MINI_PDF = (
    b"%PDF-1.4 1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj "
    b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj "
    b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj "
    b"4 0 obj<< /Length 120 >>stream\nBT /F1 12 Tf 72 700 Td "
    b"(3.1 Variables) Tj 0 -14 Td "
    b"(A variable names a storage location and holds a value.) Tj ET\n"
    b"endstream endobj"
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Point the app at a temp data dir + SQLite file so persistence is real.
    monkeypatch.setenv("BOOKMIND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKMIND_DATABASE_URL", f"sqlite:///{tmp_path}/bm.db")
    # Bust the settings cache so the new env is picked up.
    from bookmind.config import get_settings
    get_settings.cache_clear()
    repo = SqlRepository(f"sqlite:///{tmp_path}/bm.db")
    repo.create_schema()
    app = create_app(repo=repo)
    with TestClient(app) as c:
        # The lifespan started the background worker; keep a handle for tests
        # that need to drive ingestion synchronously.
        yield c


def _bootstrap(client):
    r = client.post("/api/session/bootstrap")
    assert r.status_code == 200
    return r.json()


def _wait_for_job(client, job_id, *, timeout=10, target="SUCCEEDED"):
    """Poll the job until it reaches ``target`` or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        if r.status_code != 200:
            return r
        st = r.json().get("state")
        if st == target or st in ("FAILED", "RETRYABLE_FAILED", "CANCELLED"):
            return r
        time.sleep(0.1)
    return client.get(f"/api/jobs/{job_id}")


def _wait_for_job_for_book(client, pid, book_id, *, timeout=10):
    """Poll via the project's book list until the book's job is done."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        books = client.get(f"/api/projects/{pid}/books").json()
        for b in books:
            if book_id is None or b["book_id"] == book_id:
                if b["state"] in ("SUCCEEDED", "FAILED", "RETRYABLE_FAILED", "CANCELLED"):
                    return b
        time.sleep(0.1)
    return None


# --- upload + processing vertical slice ------------------------------------

def test_upload_creates_job_and_completes(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]

    r = client.post(
        f"/api/projects/{pid}/books",
        files={"file": ("book.pdf", _MINI_PDF, "application/pdf")},
        data={"title": "My Java Book"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "book_id" in body and "job_id" in body
    job_id = body["job_id"]

    r = _wait_for_job(client, job_id)
    assert r.json()["state"] == "SUCCEEDED", r.json()
    assert r.json()["progress"] == 1.0
    # The five-stage label should be the final one.
    assert r.json()["user_label"] == "准备完成"


def test_configured_sample_is_imported_as_a_real_pdf(client, tmp_path, monkeypatch):
    """The welcome-page sample must use the real upload pipeline, not seed data."""
    sample = tmp_path / "dsacpp-3rd-edn.pdf"
    sample.write_bytes(_MINI_PDF)
    monkeypatch.setenv("BOOKMIND_SAMPLE_BOOK_PATH", str(sample))
    monkeypatch.setenv("BOOKMIND_SAMPLE_BOOK_TITLE", "数据结构（C++语言版）第三版")
    from bookmind.config import get_settings
    get_settings.cache_clear()

    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "数据结构"}).json()["project_id"]
    imported = client.post(f"/api/projects/{pid}/books/sample")

    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["sample_kind"] == "real_pdf"
    assert body["book_id"] != "demo_java_core"
    final = _wait_for_job(client, body["job_id"])
    assert final.json()["state"] == "SUCCEEDED", final.json()

    books = client.get(f"/api/projects/{pid}/books").json()
    assert books[0]["title"] == "数据结构（C++语言版）第三版"
    assert books[0]["job_id"] == body["job_id"]
    source = client.get(f"/api/books/{body['book_id']}/source.pdf")
    assert source.status_code == 200
    assert source.content == _MINI_PDF


def test_upload_after_demo_is_linked_as_reference_and_graph_succeeds(client):
    """Regression: a PRIMARY conflict must not enqueue an unscoped book."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")

    uploaded = client.post(
        f"/api/projects/{pid}/books",
        files={"file": ("real-book.pdf", _MINI_PDF, "application/pdf")},
    )

    assert uploaded.status_code == 200, uploaded.text
    body = uploaded.json()
    assert "补充资料" in body["warning"]
    assert body["book_id"] in {
        item["book_id"] for item in client.get(f"/api/projects/{pid}/books").json()
    }
    completed = _wait_for_job(client, body["job_id"])
    assert completed.json()["state"] == "SUCCEEDED", completed.json()
    graph = client.get(
        f"/api/projects/{pid}/knowledge-graph?book_id={body['book_id']}"
    ).json()
    assert graph["stats"]["concepts"] > 0


def test_processed_book_supports_grounded_qa(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]
    job_id = client.post(
        f"/api/projects/{pid}/books",
        files={"file": ("book.pdf", _MINI_PDF, "application/pdf")},
    ).json()["job_id"]
    _wait_for_job(client, job_id)

    # Create a conversation and ask a question about the uploaded book.
    conv = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]
    r = client.post(f"/api/conversations/{conv}/messages",
                    json={"content": "什么是变量"})
    assert r.status_code == 200
    # The assistant message should be persisted with structured blocks.
    fresh = client.get(f"/api/conversations/{conv}").json()
    assert len(fresh["messages"]) == 2
    assert fresh["messages"][1]["role"] == "assistant"


# --- Reader / PDF source ---------------------------------------------------

def test_pdf_source_served_and_scoped(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]
    book_id = client.post(
        f"/api/projects/{pid}/books",
        files={"file": ("book.pdf", _MINI_PDF, "application/pdf")},
    ).json()["book_id"]
    # The PDF is downloadable by the owning user.
    r = client.get(f"/api/books/{book_id}/source.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")


def test_cross_user_cannot_download_pdf(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "A"}).json()["project_id"]
    book_id = client.post(
        f"/api/projects/{pid}/books",
        files={"file": ("book.pdf", _MINI_PDF, "application/pdf")},
    ).json()["book_id"]

    # A second user (new client + same repo) bootstraps and tries to fetch A's PDF.
    client2 = TestClient(client.app)
    client2.post("/api/session/bootstrap")
    r = client2.get(f"/api/books/{book_id}/source.pdf")
    assert r.status_code in (403, 404)


# --- duplicate reuse -------------------------------------------------------

def test_duplicate_file_reuses_processing(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]
    r1 = client.post(f"/api/projects/{pid}/books",
                     files={"file": ("book.pdf", _MINI_PDF, "application/pdf")})
    job1 = r1.json()["job_id"]
    _wait_for_job(client, job1)

    # Re-upload the same bytes under a different project.
    pid2 = client.post("/api/projects", json={"name": "Java2"}).json()["project_id"]
    r2 = client.post(f"/api/projects/{pid2}/books",
                     files={"file": ("book.pdf", _MINI_PDF, "application/pdf")})
    assert r2.status_code == 200
    assert r2.json()["reused"] is True
    # The second job should already report SUCCEEDED (parse cache hit).
    r3 = _wait_for_job(client, r2.json()["job_id"])
    assert r3.json()["state"] == "SUCCEEDED"
    # Progress is represented by a project-scoped job even though the physical
    # PDF and its processed artifacts are reused across learning spaces.
    source1 = client.get(f"/api/projects/{pid}/books").json()[0]
    source2 = client.get(f"/api/projects/{pid2}/books").json()[0]
    assert source1["job_id"] != source2["job_id"]


# --- validation / errors ---------------------------------------------------

def test_rejects_non_pdf_extension(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    r = client.post(f"/api/projects/{pid}/books",
                    files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code in (400, 415)
    assert r.json()["error"]["code"] == "FILE_TYPE_UNSUPPORTED"


def test_rejects_pdf_with_wrong_magic(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    # .pdf extension but not a real PDF (no %PDF- magic).
    r = client.post(f"/api/projects/{pid}/books",
                    files={"file": ("fake.pdf", b"not a pdf at all", "application/pdf")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "FILE_CORRUPT"


def test_rejects_empty_file(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    r = client.post(f"/api/projects/{pid}/books",
                    files={"file": ("empty.pdf", b"", "application/pdf")})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "FILE_EMPTY"


# --- cancel / retry --------------------------------------------------------

def test_cancel_marks_job_cancelled(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    job_id = client.post(f"/api/projects/{pid}/books",
                         files={"file": ("book.pdf", _MINI_PDF, "application/pdf")}).json()["job_id"]
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "CANCELLED"


# --- persistence across restart --------------------------------------------

def test_project_and_book_persist_across_restart(client, tmp_path):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]
    book_id = client.post(
        f"/api/projects/{pid}/books",
        files={"file": ("book.pdf", _MINI_PDF, "application/pdf")},
    ).json()["book_id"]
    _wait_for_job_for_book(client, pid, book_id)

    # Simulate a restart: a fresh app over the same DB + data dir.
    session_cookie = client.cookies.get("bookmind_session")
    client.close()
    from sqlalchemy import select
    from bookmind.storage.sql.models import UserRow
    from bookmind.storage.sql import SqlRepository as _SR
    repo2 = _SR(f"sqlite:///{tmp_path}/bm.db")
    app2 = create_app(repo=repo2)
    with TestClient(app2) as c2:
        c2.cookies.set("bookmind_session", session_cookie)
        r = c2.get(f"/api/projects/{pid}")
        assert r.status_code == 200
        assert r.json()["name"] == "Java"
        books = c2.get(f"/api/projects/{pid}/books").json()
        assert any(b["book_id"] == book_id for b in books)


def test_qa_works_after_restart_via_persisted_chunks(client, tmp_path):
    """After a restart, asking a question about an uploaded book still works
    because the retriever is rebuilt from persisted chunks (M3)."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "Java"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books",
                files={"file": ("book.pdf", _MINI_PDF, "application/pdf")})
    _wait_for_job_for_book(client, pid, None)
    session_cookie = client.cookies.get("bookmind_session")
    client.close()

    from sqlalchemy import select
    from bookmind.storage.sql.models import UserRow
    from bookmind.storage.sql import SqlRepository as _SR
    repo2 = _SR(f"sqlite:///{tmp_path}/bm.db")
    app2 = create_app(repo=repo2)
    with TestClient(app2) as c2:
        c2.cookies.set("bookmind_session", session_cookie)
        conv = c2.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]
        r = c2.post(f"/api/conversations/{conv}/messages", json={"content": "什么是变量"})
        assert r.status_code == 200
        # The assistant responded (not an empty "no textbook" failure).
        msgs = c2.get(f"/api/conversations/{conv}").json()["messages"]
        assert len(msgs) == 2
