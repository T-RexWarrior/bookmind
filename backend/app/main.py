from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import fitz
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import learning, llm, pdf_pages, store as db

TASKS: dict[str, asyncio.Task] = {}


@asynccontextmanager
async def lifespan(app):
    db.init()
    yield
    for task in TASKS.values():
        task.cancel()
    if TASKS:
        await asyncio.gather(*TASKS.values(), return_exceptions=True)


app = FastAPI(title="BookMind · 学迹", version="0.1.0", lifespan=lifespan)


class SpaceInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    goal: str = Field(default="", max_length=3000)


class FocusInput(BaseModel):
    material_id: str
    page: int = Field(ge=1)


class SettingsInput(BaseModel):
    base_url: str = Field(min_length=8, max_length=300)
    model: str = Field(min_length=1, max_length=100)
    api_key: str = Field(default="", max_length=500)
    temperature: float = Field(default=0.4, ge=0, le=1.5)
    max_tokens: int = Field(default=6000, ge=1024, le=16000)
    timeout: int = Field(default=180, ge=30, le=600)


class LearningInput(BaseModel):
    material_id: str | None = None
    page: int = Field(default=1, ge=1)
    scope: Literal["page", "material"] = "page"
    selection: str = Field(default="", max_length=5000)
    message: str = Field(default="", max_length=6000)
    topic: str = Field(default="", max_length=500)
    count: int = Field(default=3, ge=1, le=6)


class GoalInput(BaseModel):
    goal: str = Field(min_length=2, max_length=3000)


class AnswerInput(BaseModel):
    question_id: str
    answer: int = Field(ge=0, le=3, strict=True)


class NoteInput(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    content: str = Field(default="", max_length=100000)
    source: dict = Field(default_factory=dict)


@app.get("/api/health")
def health():
    return {"ok": True, "app": "bookmind", "version": "0.1.0"}


@app.get("/api/settings")
def get_settings():
    cfg = llm.settings()
    cfg["has_api_key"] = bool(cfg.pop("api_key", ""))
    return cfg


def validated_config(body):
    values = body.model_dump()
    if not re.match(r"^https?://[^\s]+$", values["base_url"]):
        raise HTTPException(422, "API 地址需要以 http:// 或 https:// 开头。")
    values["base_url"] = values["base_url"].strip().rstrip("/")
    values["model"] = values["model"].strip()
    if not values["api_key"].strip():
        values["api_key"] = llm.settings()["api_key"]
    else:
        values["api_key"] = values["api_key"].strip()
    return values


@app.put("/api/settings")
def put_settings(body: SettingsInput):
    llm.save_settings(validated_config(body))
    return get_settings()


@app.post("/api/settings/test")
async def test_settings(body: SettingsInput):
    config = validated_config(body)
    try:
        answer = await llm.complete([{"role": "user", "content": "只回复：连接成功"}], config=config, max_tokens=1024)
        return {"ok": True, "model": config["model"], "reply": answer[:100]}
    except Exception as e:
        raise HTTPException(400, llm.safe_error(e)) from None


@app.get("/api/spaces")
def spaces():
    return db.all("SELECT s.*, (SELECT count(*) FROM materials m WHERE m.space_id=s.id) AS material_count,(SELECT count(*) FROM notes n WHERE n.space_id=s.id) AS note_count FROM spaces s ORDER BY updated DESC")


@app.post("/api/spaces")
def new_space(body: SpaceInput):
    sid = db.uid()
    db.execute("INSERT INTO spaces VALUES(?,?,?,?,?,?,?)", (sid, body.title, body.goal, None, 1, db.now(), db.now()))
    return learning.require_space(sid)


@app.get("/api/spaces/{sid}")
def get_space(sid: str):
    row = learning.require_space(sid)
    row["materials"] = [db.material(m) for m in db.all("SELECT * FROM materials WHERE space_id=? ORDER BY created", (sid,))]
    row["progress"] = learning.progress(sid)
    return row


@app.put("/api/spaces/{sid}/focus")
def focus(sid: str, body: FocusInput):
    mat = learning.require_material(sid, body.material_id)
    if body.page > mat["page_count"]:
        raise HTTPException(422, "页码超出资料范围。")
    db.execute("UPDATE spaces SET focus_material_id=?,focus_page=?,updated=? WHERE id=?", (body.material_id, body.page, db.now(), sid))
    return {"ok": True}


def text_pages(text):
    if not text.strip():
        raise HTTPException(422, "文件中没有可读取的文字。")
    # Sections are stable reading units; very long sections are split to bound prompts.
    pieces = re.split(r"(?m)(?=^#{1,2}\s)", text)
    pages = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        heading = re.search(r"(?m)^#+\s+(.+)", piece)
        title = heading.group(1)[:120] if heading else "正文"
        for index in range(0, len(piece), 12000):
            pages.append({"title": title + (f"（续 {index//12000}）" if index else ""), "text": piece[index:index+12000]})
    return pages


@app.post("/api/spaces/{sid}/materials")
async def upload(sid: str, file: UploadFile = File(...)):
    learning.require_space(sid)
    name = Path(file.filename or "资料").name
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".txt", ".md", ".markdown"}:
        raise HTTPException(422, "第一版支持 PDF、Markdown 和 TXT 文件。")
    limit = 80 * 1024 * 1024
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(413, "文件超过 80MB，请拆分后上传。")
    sha = hashlib.sha256(content).hexdigest()
    existing = db.material(db.one("SELECT * FROM materials WHERE space_id=? AND sha=?", (sid, sha)))
    if existing:
        return {**existing, "warning": "这份资料已存在，已打开原资料。"}
    warning = ""
    if suffix == ".pdf":
        try:
            with fitz.open(stream=bytes(content), filetype="pdf") as doc:
                if doc.needs_pass:
                    raise ValueError("PDF 有密码，请先解除密码。")
                if len(doc) > 2000:
                    raise ValueError("PDF 超过2000页，请先拆分。")
                pages = [{"title": f"第 {i+1} 页", "text": p.get_text()} for i, p in enumerate(doc)]
                outline = [{"title": t, "page": p, "level": level} for level, t, p in doc.get_toc() if 1 <= p <= len(doc)]
                if not any(p["text"].strip() for p in pages):
                    warning = "这是扫描版 PDF，可阅读原文件，但第一版没有 OCR，暂时不能基于它问答或出题。"
        except Exception as e:
            raise HTTPException(422, "无法解析 PDF：" + llm.safe_error(e)) from None
        filename = db.uid() + ".pdf"
        (db.DATA / "uploads" / filename).write_bytes(content)
        kind = "pdf"
    else:
        try:
            text = bytes(content).decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = bytes(content).decode("gb18030")
            except UnicodeDecodeError:
                raise HTTPException(422, "文字文件编码无法识别，请另存为 UTF-8。") from None
        pages = text_pages(text)
        outline = [{"title": p["title"], "page": i+1, "level": 1} for i, p in enumerate(pages)]
        filename = None
        kind = "markdown"
    if not pages:
        raise HTTPException(422, "资料没有内容。")
    mat = db.new_material(sid, Path(name).stem[:100], kind, pages, outline, sha, filename)
    return {**mat, "warning": warning}


@app.get("/api/spaces/{sid}/materials/{mid}/pages/{page}")
def get_page(sid: str, mid: str, page: int):
    learning.require_material(sid, mid)
    row = db.one("SELECT * FROM pages WHERE material_id=? AND number=?", (mid, page))
    if not row:
        raise HTTPException(404, "资料页码不存在。")
    return row


@app.get("/files/{sid}/{mid}")
def get_file(sid: str, mid: str):
    _, path = pdf_pages.pdf_source(sid, mid)
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/spaces/{sid}/materials/{mid}/pages/{page}/image")
def pdf_page_image(sid: str, mid: str, page: int, width: int = Query(default=1440, ge=320, le=2400)):
    path = pdf_pages.page_image(sid, mid, page, width)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.get("/api/spaces/{sid}/materials/{mid}/pages/{page}/layout")
def pdf_page_layout(sid: str, mid: str, page: int):
    return pdf_pages.page_layout(sid, mid, page)


async def runner(jid, handler, sid, body):
    try:
        db.update_job(jid, status="running")
        result = await handler(sid, body, jid)
        db.update_job(jid, status="completed", stage="已完成", result=result)
        db.execute("UPDATE spaces SET updated=? WHERE id=?", (db.now(), sid))
    except asyncio.CancelledError:
        db.update_job(jid, status="failed", error="任务随服务关闭而中断，请重试。", stage="已中断")
        raise
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else llm.safe_error(e)
        db.update_job(jid, status="failed", stage="未完成", error=str(detail))
    finally:
        TASKS.pop(jid, None)


def launch(sid, kind, body, handler):
    learning.require_space(sid)
    if not llm.settings().get("api_key"):
        raise HTTPException(422, "请先在 API 设置中填写密钥。")
    active = db.one("SELECT id FROM jobs WHERE space_id=? AND status IN ('running','queued')", (sid,))
    if active:
        raise HTTPException(409, "这个学习空间还有任务正在进行，请等待完成。")
    jid = db.uid()
    db.execute("INSERT INTO jobs(id,space_id,kind,status,stage,created,updated) VALUES(?,?,?,?,?,?,?)", (jid, sid, kind, "queued", "准备中", db.now(), db.now()))
    TASKS[jid] = asyncio.create_task(runner(jid, handler, sid, body))
    return {"job_id": jid}


@app.post("/api/spaces/{sid}/chat")
async def chat(sid: str, body: LearningInput):
    if not body.message.strip():
        raise HTTPException(422, "请先输入问题。")
    learning.source_context(sid, body.model_dump(), body.message)
    return launch(sid, "chat", body.model_dump(), learning.chat)


@app.post("/api/spaces/{sid}/visualize")
async def visualize(sid: str, body: LearningInput):
    if not body.message.strip():
        raise HTTPException(422, "请描述想理解的概念或过程。")
    learning.source_context(sid, body.model_dump(), body.message)
    return launch(sid, "visualize", body.model_dump(), learning.visualize)


@app.post("/api/spaces/{sid}/handbook")
async def handbook(sid: str, body: GoalInput):
    return launch(sid, "handbook", body.model_dump(), learning.handbook)


@app.post("/api/spaces/{sid}/quizzes")
async def quizzes(sid: str, body: LearningInput):
    learning.source_context(sid, body.model_dump(), body.topic)
    return launch(sid, "quiz", body.model_dump(), learning.quiz)


@app.get("/api/spaces/{sid}/jobs")
def jobs(sid: str):
    learning.require_space(sid)
    return [db.decode(r, "result") for r in db.all("SELECT * FROM jobs WHERE space_id=? ORDER BY created DESC LIMIT 12", (sid,))]


@app.get("/api/spaces/{sid}/jobs/{jid}")
def job(sid: str, jid: str):
    row = db.decode(db.one("SELECT * FROM jobs WHERE id=? AND space_id=?", (jid, sid)), "result")
    if not row:
        raise HTTPException(404, "任务不存在。")
    return row


@app.get("/api/spaces/{sid}/messages")
def messages(sid: str):
    learning.require_space(sid)
    return [db.decode(m, "refs") for m in db.all("SELECT * FROM messages WHERE space_id=? ORDER BY created", (sid,))]


@app.get("/api/spaces/{sid}/artifacts/{aid}")
def artifact(sid: str, aid: str):
    row = db.decode(db.one("SELECT * FROM artifacts WHERE id=? AND space_id=?", (aid, sid)), "payload", "refs")
    if not row:
        raise HTTPException(404, "图解不存在。")
    if row["format"] == "svg":
        row["payload"]["code"] = learning.sanitize_svg(row["payload"]["code"])
    return row


@app.get("/api/spaces/{sid}/quizzes")
def list_quizzes(sid: str):
    learning.require_space(sid)
    return [learning.public_quiz(sid, q["id"]) for q in db.all("SELECT id FROM quizzes WHERE space_id=? ORDER BY created DESC", (sid,))]


@app.get("/api/spaces/{sid}/quizzes/{qid}")
def get_quiz(sid: str, qid: str):
    return learning.public_quiz(sid, qid)


@app.post("/api/spaces/{sid}/quizzes/{qid}/answer")
def answer(sid: str, qid: str, body: AnswerInput):
    return learning.grade(sid, qid, body.question_id, body.answer)


@app.post("/api/spaces/{sid}/quizzes/{qid}/hints/{question_id}")
def hint(sid: str, qid: str, question_id: str):
    quiz = learning.get_quiz(sid, qid)
    if not any(q["id"] == question_id for q in quiz["questions"]):
        raise HTTPException(404, "题目不存在。")
    db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,?)", (qid, question_id, db.now()))
    return learning.public_quiz(sid, qid)


@app.get("/api/spaces/{sid}/progress")
def progress(sid: str):
    return learning.progress(sid)


def validate_source(sid, source):
    if source.get("material_id"):
        mat = learning.require_material(sid, source["material_id"])
        if not 1 <= int(source.get("page", 1)) <= mat["page_count"]:
            raise HTTPException(422, "笔记来源的页码不存在。")
    if source.get("artifact_id"):
        artifact(sid, source["artifact_id"])
    if source.get("quiz_id"):
        learning.get_quiz(sid, source["quiz_id"])
    if source.get("message_id") and not db.one("SELECT id FROM messages WHERE id=? AND space_id=?", (source["message_id"], sid)):
        raise HTTPException(422, "笔记来源的对话不存在。")


@app.get("/api/spaces/{sid}/notes")
def notes(sid: str, q: str = ""):
    learning.require_space(sid)
    rows = db.all("SELECT * FROM notes WHERE space_id=? AND (title LIKE ? OR content LIKE ?) ORDER BY updated DESC", (sid, "%"+q+"%", "%"+q+"%"))
    return [db.decode(n, "source") for n in rows]


@app.post("/api/spaces/{sid}/notes")
def new_note(sid: str, body: NoteInput):
    learning.require_space(sid)
    validate_source(sid, body.source)
    nid = db.uid()
    db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?)", (nid, sid, body.title, body.content, db.dump(body.source), db.now(), db.now()))
    return db.decode(db.one("SELECT * FROM notes WHERE id=?", (nid,)), "source")


@app.put("/api/spaces/{sid}/notes/{nid}")
def edit_note(sid: str, nid: str, body: NoteInput):
    if not db.one("SELECT id FROM notes WHERE id=? AND space_id=?", (nid, sid)):
        raise HTTPException(404, "笔记不存在。")
    validate_source(sid, body.source)
    db.execute("UPDATE notes SET title=?,content=?,source=?,updated=? WHERE id=?", (body.title, body.content, db.dump(body.source), db.now(), nid))
    return db.decode(db.one("SELECT * FROM notes WHERE id=?", (nid,)), "source")


@app.delete("/api/spaces/{sid}/notes/{nid}")
def delete_note(sid: str, nid: str):
    learning.require_space(sid)
    db.execute("DELETE FROM notes WHERE id=? AND space_id=?", (nid, sid))
    return {"ok": True}


DIST = db.ROOT / "frontend" / "dist"


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith(("api/", "files/")):
        raise HTTPException(404, "接口不存在。")
    target = (DIST / path).resolve()
    if target.is_relative_to(DIST.resolve()) and target.is_file():
        return FileResponse(target)
    if (DIST / "index.html").exists():
        return FileResponse(DIST / "index.html")
    raise HTTPException(404, "前端尚未构建，请先运行 start.ps1。")
