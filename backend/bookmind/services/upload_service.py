"""Upload service — file validation and on-disk storage (PRODUCTIZATION §5.2,
§9.4, §11.3).

Receives a raw upload, performs the size/type/extension/file-header triple
check, and stores the original under a server-generated path so the user's
filename is never used as a disk path. Parsing/OCR happens later in the
ingestion pipeline; this layer only validates and persists the raw bytes.

Storage layout (§9.4)::

    {data_dir}/uploads/{user_id}/{book_id}/source.pdf

Failures raise :class:`~bookmind.errors.AppError` with the §5.2 user copy
and a stable ``code`` the frontend branches on.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from ..errors import AppError
from ..config import Settings, get_settings

# PDF magic number — every valid PDF starts with ``%PDF-``.
_PDF_MAGIC = b"%PDF-"


class UploadService:
    """Validate and store uploaded textbook files."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- validation --------------------------------------------------------

    def validate(self, file_bytes: bytes, filename: str, content_type: str = "") -> None:
        """Run all four checks; raise :class:`AppError` on the first failure.

        Checks: size, extension, MIME, file-header magic. The §5.2 user-facing
        copy is the message; ``code`` lets the frontend pick the right card.
        """
        self.validate_metadata(len(file_bytes), file_bytes[:5], filename, content_type)

    def validate_metadata(
        self, size: int, header: bytes, filename: str, content_type: str = "",
    ) -> None:
        """Validate a streamed upload without loading its body into memory."""
        max_bytes = self.settings.max_upload_mb * 1024 * 1024
        if size > max_bytes:
            raise AppError(
                "FILE_TOO_LARGE",
                f"文件过大，当前上限 {self.settings.max_upload_mb} MB。请压缩或拆分后重试。",
                status_code=413, can_retry=False, action="REPLACE_FILE",
            )
        if size <= 0:
            raise AppError(
                "FILE_EMPTY",
                "文件为空，请确认上传的是完整的 PDF 资料。",
                status_code=400, can_retry=False, action="REPLACE_FILE",
            )
        name = (filename or "").lower()
        if not name.endswith(".pdf"):
            raise AppError(
                "FILE_TYPE_UNSUPPORTED",
                "当前版本先支持 PDF 资料，PPT、文档和网页将在统一资料接口中陆续接入。",
                status_code=415, can_retry=False, action="REPLACE_FILE",
            )
        # MIME is advisory only — browsers can lie. The magic-number check
        # below is authoritative. We still reject obviously wrong MIME early.
        if content_type and content_type != "application/pdf" and not content_type.startswith(
            "application/pdf"
        ) and content_type != "application/octet-stream":
            raise AppError(
                "FILE_TYPE_UNSUPPORTED",
                "文件类型不是 PDF，请上传 .pdf 文件。",
                status_code=415, can_retry=False, action="REPLACE_FILE",
            )
        if not header.startswith(_PDF_MAGIC):
            raise AppError(
                "FILE_CORRUPT",
                "文件无法打开，请确认原文件可以正常阅读后重试。",
                status_code=400, can_retry=False, action="REPLACE_FILE",
            )

    # --- storage -----------------------------------------------------------

    def upload_dir(self, user_id: str, book_id: str) -> Path:
        return Path(self.settings.data_dir) / "uploads" / user_id / book_id

    def create_staging_path(self, user_id: str) -> Path:
        directory = Path(self.settings.data_dir) / "uploads" / ".staging" / user_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{uuid.uuid4().hex}.pdf"

    def adopt_staged(self, staged: Path, user_id: str, book_id: str) -> Path:
        """Atomically move a validated temporary upload into its final path."""
        target_dir = self.upload_dir(user_id, book_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "source.pdf"
        os.replace(staged, target)
        return target

    def save(self, user_id: str, book_id: str, file_bytes: bytes, filename: str) -> Path:
        """Persist the raw PDF under the server-generated path. Returns the path."""
        target_dir = self.upload_dir(user_id, book_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "source.pdf"
        with open(path, "wb") as f:
            f.write(file_bytes)
        return path

    def path_for(self, user_id: str, book_id: str) -> Path:
        return self.upload_dir(user_id, book_id) / "source.pdf"

    def exists(self, user_id: str, book_id: str) -> bool:
        return self.path_for(user_id, book_id).is_file()

    def delete(self, user_id: str, book_id: str) -> None:
        """Remove the stored source PDF and its directory (best-effort)."""
        d = self.upload_dir(user_id, book_id)
        if d.is_dir():
            for child in d.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass
