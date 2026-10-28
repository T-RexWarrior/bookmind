"""Bridge completed native Books into the shared textbook learning workflow.

Generation stays in BookMind BookEngine. This adapter freezes its Markdown
export as a reading material, then reuses BookMind's normal textbook binding.
The snapshot is immutable: opening it again cannot reset notes or assessments.
"""
from __future__ import annotations

import time

from bookmind.book.export import export_filename, render_book_markdown
from bookmind.book.models import BlockStatus, BookStatus, PageStatus
from bookmind.reading import ReadingStore
from bookmind.services.file_io import atomic_write_text

from . import repository, service


def import_generated_book(native_book_id, engine):
    with repository.binding_lock:
        # Resolve with the native storage first (including id validation).
        book = engine.load_book(native_book_id)
        if book is None:
            raise FileNotFoundError('生成的学习资料不存在')
        previous = repository.generated_source(native_book_id=book.id)
        if previous:
            return service.overview(service.find_book(material_id=previous['material_id']))

        pages = engine.list_pages(book.id)
        spine = engine.load_spine(book.id)
        if book.status != BookStatus.READY or not pages or any(p.status != PageStatus.READY for p in pages):
            raise ValueError('学习资料还未完整生成，请先完成或重试失败章节')
        if not spine or any(not c.page_ids for c in spine.chapters):
            raise ValueError('学习资料缺少完整的章节目录')
        if any(block.status in {BlockStatus.ERROR, BlockStatus.PENDING, BlockStatus.GENERATING} for p in pages for block in p.blocks):
            raise ValueError('仍有未完成的内容，请先重试生成')
        page_ids = {page.id for page in pages}
        if any(not set(chapter.page_ids) <= page_ids for chapter in spine.chapters):
            raise ValueError('有章节正文缺失，请先完成生成')
        if not any(not getattr(c, 'auto_overview', False) and c.page_ids for c in spine.chapters):
            raise ValueError('请至少生成一个正文章节')

        markdown = render_book_markdown(book, spine, pages)
        markdown += f'\n<!-- BookMind Books source: {book.id}; revision: {book.revision} -->\n'
        reading = ReadingStore()
        source_dir = reading.root / 'generated_sources' / book.id
        source_dir.mkdir(parents=True, exist_ok=True)
        source_path = source_dir / export_filename(book)
        atomic_write_text(source_path, markdown)
        manifest = reading.ingest(source_path)
        textbook = service.bind_material(manifest.material_id)
        inputs = engine.storage.load_inputs(book.id)
        repository.insert_generated_source(
            (book.id, manifest.material_id, book.revision, inputs.user_intent if inputs else '', time.time()))
        return service.overview(service.find_book(id=textbook['id']))
