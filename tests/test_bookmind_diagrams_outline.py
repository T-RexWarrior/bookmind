import asyncio
import json
from types import SimpleNamespace

import pytest

from deeptutor.bookmind.study_diagrams import BookMindStudyDiagrams
from deeptutor.capabilities.reading.capability import ReadingCapability
from deeptutor.core.context import UnifiedContext
from deeptutor.reading.extract import _pdf_outline
from deeptutor.reading.outline_titles import clean_outline_title
from deeptutor.visualizers.tool import SubmitVisualizationTool
from deeptutor.visualizers.protocol import VISUALIZE_MODE_KEY


@pytest.mark.parametrize('raw,expected', [
    ('\uf06e 数组求和', '数组求和'), ('  \uf06e\uf0b7 线性递归\n', '线性递归'),
    ('§1.4 *递归', '§1.4 *递归'), ('O(n²) 与 ∑、∀', 'O(n²) 与 ∑、∀'),
    ('范围 \uf06e 内部符号', '范围 \uf06e 内部符号'), ('\uf123 自定义字符', '\uf123 自定义字符'),
])
def test_portable_outline_labels_preserve_meaning(raw, expected):
    assert clean_outline_title(raw) == expected
    assert clean_outline_title(expected) == expected


def test_pdf_extraction_cleans_only_decorative_bookmark_labels():
    doc = SimpleNamespace(get_toc=lambda: [[1, '§1.4 *递归', 38], [2, '\uf06e 数组求和', 39],
                                         [2, '\uf06e', 39], [2, '\uf06e 错误页', 700]])
    rows = _pdf_outline(doc, page_count=669)
    assert [(row.locator, row.level, row.title) for row in rows] == [
        (38, 1, '§1.4 *递归'), (39, 2, '数组求和')]


def test_cached_outline_cleaning_needs_no_reimport_or_source_rewrite(tmp_path, monkeypatch):
    from deeptutor.reading import ReadingStore
    from deeptutor.services.path_service import PathService

    monkeypatch.setenv('DEEPTUTOR_HOME', str(tmp_path))
    PathService.reset_instance()
    try:
        source = tmp_path / 'notes.md'
        source.write_text('# 标题\n\n原文 \uf06e 保持不动。', encoding='utf-8')
        store = ReadingStore()
        manifest = store.ingest(source)
        outline_file = store._dir(manifest.material_id) / 'outline.json'
        old_rows = [{'locator': 1, 'title': '\uf06e 数组求和', 'level': 2, 'source_anchor': 'unchanged'}]
        outline_file.write_text(json.dumps(old_rows), encoding='utf-8')
        text_before = store.unit_text(manifest.material_id, 1)
        rows = store.outline(manifest.material_id)
        assert rows[0].title == '数组求和'
        assert rows[0].source_anchor == 'unchanged' and rows[0].locator == 1 and rows[0].level == 2
        assert store.unit_text(manifest.material_id, 1) == text_before
        assert json.loads(outline_file.read_text()) == old_rows
    finally:
        PathService.reset_instance()


@pytest.mark.parametrize('mode', ['immersive_reading_mode', 'mastery_mode'])
def test_native_diagrams_are_available_only_in_bookmind_study(monkeypatch, mode):
    cap = BookMindStudyDiagrams()
    context = UnifiedContext(metadata={mode: True})
    monkeypatch.delenv('BOOKMIND_APP', raising=False)
    assert not cap.is_active(context)
    monkeypatch.setenv('BOOKMIND_APP', '1')
    assert cap.is_active(context)
    assert cap.owned_tools == ('submit_visualization',)
    assert not cap.is_active(UnifiedContext())
    assert not cap.is_active(UnifiedContext(metadata={mode: True, VISUALIZE_MODE_KEY: True}))
    assert 'view_figure' not in ReadingCapability().owned_tools
    monkeypatch.delenv('BOOKMIND_APP')
    assert 'view_figure' in ReadingCapability().owned_tools


def test_inline_diagram_uses_native_validation_and_persists_the_envelope(tmp_path, monkeypatch):
    from deeptutor.services.path_service import PathService

    monkeypatch.setenv('DEEPTUTOR_HOME', str(tmp_path))
    monkeypatch.setenv('BOOKMIND_APP', '1')
    PathService.reset_instance()
    try:
        context = UnifiedContext(metadata={'immersive_reading_mode': True})
        cap = BookMindStudyDiagrams()
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200"><title>二叉树</title><desc>根与左右孩子</desc><circle cx="150" cy="40" r="20"/></svg>'
        kwargs = cap.augment_kwargs('submit_visualization', {'visualizer': 'svg', 'payload': svg,
            'title': '二叉树', 'description': '两个孩子', 'alt_text': '根与孩子'}, context)
        tool = SubmitVisualizationTool()
        accepted = asyncio.run(tool.execute(**kwargs))
        assert accepted.success
        envelope = accepted.metadata['bookmind_diagram']
        assert envelope['render_type'] == 'svg'
        assert envelope['payload']['data'] == svg
        assert envelope['presentation']['title'] == '二叉树'
        ordinary = asyncio.run(tool.execute(**{**kwargs, '_bookmind_inline': False}))
        assert ordinary.success and 'bookmind_diagram' not in ordinary.metadata
        broken = asyncio.run(tool.execute(**{**kwargs, 'payload': 'A / B / C'}))
        assert not broken.success and 'bookmind_diagram' not in broken.metadata
        prompt = cap.system_block(context, language='zh', prompts={}).content
        assert 'ASCII' in prompt and 'submit_visualization' in prompt
    finally:
        PathService.reset_instance()


def test_diagram_survives_trace_preview_event_pressure():
    from deeptutor.services.session.event_preview import compact_trace_preview

    diagram = {'type': 'tool_result', 'metadata': {'bookmind_diagram': {'render_type': 'svg'}}}
    events = [diagram, *[{'type': 'tool_result', 'content': 'ordinary trace'} for _ in range(30)],
              {'type': 'result'}, {'type': 'done'}]
    preview, truncated = compact_trace_preview(events, max_events=3)
    assert truncated and diagram in preview
