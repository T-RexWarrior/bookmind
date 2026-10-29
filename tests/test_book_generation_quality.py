import asyncio
from types import SimpleNamespace

import pytest

from bookmind.book.blocks.base import BlockContext
from bookmind.book.models import Block, BlockStatus, BlockType, Book, BookInputs, Chapter, Page, Spine
from bookmind.book.storage import BookStorage
from bookmind.book.writing import allocate_prose_budget, book_writing_context, active_writing_context


def test_export_does_not_repeat_headings_or_hide_chart_type():
    from bookmind.book.export import render_block
    section = Block(type=BlockType.SECTION, status=BlockStatus.READY, payload={
        'subsections': [{'heading': '例子', 'body': '### 例子\n\n正文'}, {'heading': '步骤', 'body': '具体步骤'}]})
    text = render_block(section, 'zh')
    assert text.count('### 例子') == 1
    assert '### 步骤' in text
    figure = Block(type=BlockType.FIGURE, status=BlockStatus.READY,
                   payload={'render_type': 'chartjs', 'code': {'language': 'javascript', 'content': '{"type":"bar"}'}})
    assert '```chartjs' in render_block(figure, 'zh')


def test_reading_import_keeps_large_figures_and_code_fences_whole():
    from bookmind.reading.extract import split_markdown_by_headings
    svg = '<svg xmlns="http://www.w3.org/2000/svg">\n\n' + '<text>A</text>\n' * 600 + '</svg>'
    code = '# This is a code comment, not a chapter\n\n' + 'print(1)\n' * 600
    source = '# 教材\n\n## 图解\n\n```svg\n' + svg + '\n```\n\n## 代码\n\n```python\n' + code + '\n```'
    units, outline = split_markdown_by_headings(source)
    assert any('```svg\n' + svg + '\n```' in unit for unit in units)
    assert any('```python\n' + code + '\n```' in unit for unit in units)
    assert all('code comment' not in entry.title for entry in outline)
    assert all(unit.count('```') % 2 == 0 for unit in units)


def test_new_markdown_import_uses_rich_reader_without_rewriting_existing_material(native_book, tmp_path):
    from bookmind.reading import ReadingStore
    reading = ReadingStore()
    original = reading.manifest(native_book['material_id']).to_dict()
    path = tmp_path / 'rich-book.md'
    path.write_text('# Rich book\n\n## Diagram\n\n```svg\n<svg xmlns="http://www.w3.org/2000/svg"></svg>\n```', encoding='utf-8')
    fresh = reading.ingest(path)
    assert fresh.content_format == 'web_markdown'
    assert reading.manifest(native_book['material_id']).to_dict() == original


def test_section_budgets_share_chapter_limit_and_respect_explicit_length():
    blocks = [Block(type=BlockType.SECTION, params={'target_words': n}) for n in [700, 1000, 500]]
    allocate_prose_budget(blocks, 'standard', '每章控制在 500 字左右，保留代码')
    assert abs(sum(b.params['target_words'] for b in blocks) - 500) <= 1
    assert all(b.metadata['chapter_prose_budget'] == 500 for b in blocks)
    allocate_prose_budget(blocks, 'deep')
    assert abs(sum(b.params['target_words'] for b in blocks) - 4000) <= 1


def test_compiler_carries_requirements_and_previous_example(native_book):
    s = BookStorage()
    book = Book(title='实验分析')
    first, second = Chapter(title='采样', order=1), Chapter(title='统计', order=2)
    s.save_book(book)
    s.save_inputs(book.id, BookInputs(user_intent='使用同一组温度数据；只会 Python 基础'))
    s.save_spine(Spine(book_id=book.id, chapters=[first, second]))
    s.save_page(Page(book_id=book.id, chapter_id=first.id, order=1, blocks=[
        Block(type=BlockType.CODE, status=BlockStatus.READY, payload={'code': 'temperature = [20, 22, 24]'})]))
    s.save_page(Page(book_id=book.id, chapter_id=second.id, order=2))
    context = book_writing_context(s, book.id, second)
    assert '只会 Python 基础' in context
    assert 'temperature = [20, 22, 24]' in context
    assert '采样' in context and '统计' in context


def test_short_section_outline_respects_focus_total_and_shared_example(monkeypatch):
    from bookmind.book.blocks.section import SectionGenerator
    chapter = Chapter(title='线索树', learning_objectives=['定义结构', '实现遍历'])
    block = Block(type=BlockType.SECTION, params={'focus': '只讲标记位', 'target_words': 300})
    ctx = BlockContext(book_id='demo', chapter=chapter, page=Page(book_id='demo', chapter_id=chapter.id), block=block, language='zh')

    async def fake(**kwargs):
        assert 'ONLY its assigned focus' in kwargs['user_prompt']
        return {'shared_example': 'A 的左线索指向 B', 'subsections': [
            {'heading': '标记位', 'focus': '0孩子1线索', 'target_words': 900},
            {'heading': '重复定义', 'target_words': 900}]}
    monkeypatch.setattr('bookmind.book.blocks.section.llm_json', fake)
    async def scenario():
        return await SectionGenerator()._make_outline(ctx=ctx, chapter_title=chapter.title, chapter_summary='',
            objectives=chapter.learning_objectives, focus_topic='只讲标记位', section_role='core', target_words=300, rag_context='')
    outline = asyncio.run(scenario())
    assert len(outline['subsections']) == 1
    assert outline['subsections'][0]['target_words'] == 300
    assert outline['subsections'][0]['shared_example'] == 'A 的左线索指向 B'


def test_writer_context_is_scoped_and_truncated_prose_is_not_success(monkeypatch):
    from bookmind.book.blocks import _llm_writer as writer
    from bookmind.book.blocks.text import TextGenerator
    captured = []
    monkeypatch.setattr(writer, 'get_llm_config', lambda: SimpleNamespace(model='test', api_key='', base_url='', binding='openai'))
    async def complete(**kwargs):
        await asyncio.sleep(0)
        captured.append(kwargs['prompt'])
        return '正文'
    monkeypatch.setattr(writer, 'llm_complete', complete)
    async def scenario():
        async def generate(label):
            chapter = Chapter(title='测试')
            block = Block(type=BlockType.TEXT)
            page = Page(book_id=label, chapter_id=chapter.id, blocks=[block])
            await TextGenerator().generate(BlockContext(book_id=label, chapter=chapter, page=page,
                block=block, extra={'writing_context': label}))
        await asyncio.gather(generate('ONLY_BOOK_A'), generate('ONLY_BOOK_B'))
        assert active_writing_context.get() == ''
    asyncio.run(scenario())
    assert len(captured) == 2
    assert all(('ONLY_BOOK_A' in prompt) != ('ONLY_BOOK_B' in prompt) for prompt in captured)
    async def truncated(**kwargs):
        kwargs['outcome'].finish_reason = 'length'
        yield '不完整的正文'
    monkeypatch.setattr(writer, 'llm_stream', truncated)
    with pytest.raises(RuntimeError, match='truncated'):
        asyncio.run(writer.llm_text(user_prompt='test', system_prompt='test', require_complete=True))
