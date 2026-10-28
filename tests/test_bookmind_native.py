import asyncio
from pathlib import Path

import pytest

from deeptutor.bookmind import service
from deeptutor.learning.storage import LearningStore
from deeptutor.reading import ReadingStore
from deeptutor.services.path_service import PathService
from deeptutor.services.notebook import notebook_manager


@pytest.mark.parametrize('surface,requested_mode', [
    ('practice', 'immersive_reading'), ('practice', 'mastery_path'),
    ('immersive_reading', None), ('immersive_reading', 'mastery_path'),
    ('mastery_path', None), ('mastery_path', 'immersive_reading'),
])
def test_conversation_scope_rejected_before_admission_writes(native_book, monkeypatch, surface, requested_mode):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from deeptutor.bookmind import practice
    from deeptutor.reading import ReadingCatalogStore
    from deeptutor.services.session import get_sqlite_session_store
    from deeptutor.services.session.turns.request_preparer import TurnRequestPreparer

    monkeypatch.setenv('BOOKMIND_APP', '1')

    async def scenario():
        store = get_sqlite_session_store()
        if surface == 'practice':
            sid = await practice.open_session(native_book)
        else:
            saved = await store.create_session(title=surface)
            sid = saved['id']
            await store.update_session_preferences(sid, {
                'workspace_mode': surface, 'reading_workspace_id': native_book['workspace_id'],
                'mastery_path_id': native_book['path_id'],
            })
        before = await store.get_session(sid)
        catalog = ReadingCatalogStore()
        rows_before = catalog.list_sessions(native_book['workspace_id'])
        admission = SimpleNamespace(store=store, _ensure_accepting_turns=AsyncMock())
        with pytest.raises(RuntimeError, match='会话属于'):
            await TurnRequestPreparer.start_turn.__wrapped__(admission, {
                'session_id': sid, 'content': 'wrong screen', 'capability': 'chat',
                'workspace_mode': requested_mode, 'reading_workspace_id': native_book['workspace_id'],
                'mastery_path_id': native_book['path_id'], 'language': 'zh', 'auto_route': False,
            })
        assert await store.get_session(sid) == before
        assert catalog.list_sessions(native_book['workspace_id']) == rows_before

    asyncio.run(scenario())


def test_inline_quizzes_keep_their_reading_or_guidance_owner(native_book):
    from deeptutor.bookmind.session_scope import assert_session_surface

    for surface, field, owner in [
        ('immersive_reading', 'reading_workspace_id', native_book['workspace_id']),
        ('mastery_path', 'mastery_path_id', native_book['path_id']),
    ]:
        preferences = {'workspace_mode': surface, field: owner}
        assert_session_surface('inline-chat', preferences, 'deep_question', surface, {field: owner})
        with pytest.raises(RuntimeError, match='另一'):
            assert_session_surface('inline-chat', preferences, 'chat', surface, {field: 'another-book'})


def test_reading_catalog_hides_foreign_modes_without_deleting_conversations(native_book, monkeypatch):
    from deeptutor.bookmind import practice
    from deeptutor.bookmind.session_scope import assert_session_surface
    from deeptutor.api.routers.reading import list_reading_sessions
    from deeptutor.reading import ReadingCatalogStore
    from deeptutor.services.session import get_sqlite_session_store

    monkeypatch.setenv('BOOKMIND_APP', '1')

    async def scenario():
        store = get_sqlite_session_store()
        catalog = ReadingCatalogStore()
        practice_id = await practice.open_session(native_book)
        point = native_book['objectives'][-1]
        assert_session_surface(practice_id, {}, 'deep_question', None,
                               {'config': {'bookmind_objective_id': point['id']}})
        saved_ids = [practice_id]
        allowed = []
        for preferences in [{}, {'workspace_mode': 'immersive_reading', 'reading_workspace_id': native_book['workspace_id']},
                            {'workspace_mode': 'mastery_path', 'mastery_path_id': native_book['path_id']},
                            {'workspace_mode': 'immersive_reading', 'reading_workspace_id': 'another-book'}]:
            session = await store.create_session(title='conversation')
            sid = session['id']
            await store.update_session_preferences(sid, preferences)
            saved_ids.append(sid)
            if not preferences or preferences.get('reading_workspace_id') == native_book['workspace_id']:
                allowed.append(sid)
        for sid in saved_ids:
            catalog.attach_session(native_book['workspace_id'], sid, title='conversation')
        listed = await list_reading_sessions(native_book['workspace_id'])
        assert {row['session_id'] for row in listed['sessions']} == set(allowed)
        for sid in saved_ids:
            assert await store.get_session(sid)
        assert len(catalog.list_sessions(native_book['workspace_id'])) == len(saved_ids)

    asyncio.run(scenario())


def test_practice_requires_a_real_point_and_matching_source(native_book):
    point = native_book['objectives'][-1]
    book, verified = service.verified_practice_objective(native_book['material_id'], point['id'], point['start'])
    assert book['id'] == native_book['id'] and verified['id'] == point['id']
    with pytest.raises(ValueError):
        service.verified_practice_objective(native_book['material_id'], 'point-from-another-book', point['start'])
    with pytest.raises(ValueError):
        service.verified_practice_objective(native_book['material_id'], point['id'], point['end'] + 1)


def test_practice_persists_selected_point_without_revealing_answer(native_book):
    from deeptutor.api.routers.reading_extensions import _persist_reading_quiz_pending
    from deeptutor.services.session import get_sqlite_session_store
    point = native_book['objectives'][-1]
    async def scenario():
        result = {'questions': [{'prompt': '中序遍历在什么时候访问根节点？', 'choices': ['最先', '左右之间', '最后', '随机'], 'correct_choice_index': 1, 'evidence': '先左子树，再根节点，最后右子树'}]}
        await _persist_reading_quiz_pending(native_book['material_id'], point['start'], result, objective_id=point['id'])
        public = result['questions'][0]
        assert 'correct_choice_index' not in public and 'evidence' not in public
        saved = await get_sqlite_session_store().get_reading_quiz_pending(native_book['material_id'], point['start'])
        assert saved[public['id']]['knowledge_point_id'] == point['id']
        assert saved[public['id']]['correct_choice_index'] == 1
    asyncio.run(scenario())


def test_optional_service_policy_applies_to_explicit_turn_tools(monkeypatch):
    from deeptutor.bookmind.policy import permitted_optional_tools
    from deeptutor.api.routers.tools import _runtime_availability
    monkeypatch.setenv('BOOKMIND_APP', '1')
    assert permitted_optional_tools(['reason', 'imagegen', 'videogen', 'web_search', 'brainstorm']) == ['reason', 'brainstorm']
    assert permitted_optional_tools(None) == []
    assert _runtime_availability('imagegen') == (False, 'bookmind_service_not_enabled')
    monkeypatch.delenv('BOOKMIND_APP')
    assert permitted_optional_tools(None) is None


def test_visualizer_catalog_excludes_unconfigured_renderers(native_book, monkeypatch):
    from deeptutor.visualizers.registry import get_visualizer_registry
    monkeypatch.setenv('BOOKMIND_APP', '1')
    registry = get_visualizer_registry()
    ids = {plugin.manifest.id for plugin in registry.catalog()}
    assert 'svg' in ids
    assert ids <= {'svg', 'chartjs', 'mermaid', 'mindmap', 'html'}
    assert registry.get('manim_video') is None


@pytest.fixture
def native_book(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPTUTOR_HOME', str(tmp_path))
    PathService.reset_instance()
    file = tmp_path / 'tree.md'
    file.write_text('# 二叉树\n\n二叉树的每个结点至多有两个孩子。\n\n## 中序遍历\n\n中序遍历先左子树，再根节点，最后右子树。\n', encoding='utf-8')
    store = ReadingStore()
    manifest = store.ingest(file)
    book = service.bind_material(manifest.material_id)
    yield book
    PathService.reset_instance()


def test_binding_reuses_three_native_aggregates(native_book):
    book = native_book
    again = service.bind_material(book['material_id'])
    assert book['id'] == again['id'] and book['path_id'] == again['path_id']
    assert book['notebook_id'] == again['notebook_id']
    assert len(service.books()) == 1
    topic = LearningStore().get_topic(book['path_id'])
    assert any(s.kind.value == 'reading' for s in topic.sources)
    assert any(s.kind.value == 'notebook' and s.source_id == book['notebook_id'] for s in topic.sources)


def test_mastery_reads_real_native_material(native_book):
    from deeptutor.learning.topic_materials import build_topic_materials
    topic = LearningStore().get_topic(native_book['path_id'])
    sources = build_topic_materials(topic.sources)
    assert any('中序遍历' in text for text in sources.source_index().values())
    assert all('教材原文' in row.full_text for row in sources.materials if row.kind == 'reading')


def test_notebook_keeps_verified_textbook_and_point(native_book):
    book = native_book
    point = book['objectives'][-1]
    notebook_manager.add_record([book['notebook_id']], 'reading', '遍历笔记', '学习摘录', '先左，再根，最后右。',
        summary='遍历顺序', metadata={'locator': point['start'], 'bookmind': {'book_id': 'forged'}})
    note = notebook_manager.get_notebook(book['notebook_id'])['records'][-1]
    assert note['metadata']['bookmind']['book_id'] == book['id']
    assert note['metadata']['bookmind']['knowledge_point_id'] == point['id']
    assert note['metadata']['bookmind']['locator'] == point['start']


def test_reading_quiz_links_to_native_retention_once_and_hides_key(native_book):
    from deeptutor.api.routers.reading_extensions import _persist_reading_quiz_pending, submit_quiz_answers, QuizAnswersPayload
    from deeptutor.services.session import get_sqlite_session_store
    book = native_book
    point = book['objectives'][-1]
    async def scenario():
        public = {'questions': [{'prompt': '中序遍历何时访问根节点？', 'choices': ['最先访问', '左右子树之间', '最后访问', '随机访问'], 'correct_choice_index': 1, 'evidence': '先左子树，再根节点，最后右子树'}]}
        await _persist_reading_quiz_pending(book['material_id'], point['start'], public)
        question = public['questions'][0]
        assert 'correct_choice_index' not in question and 'evidence' not in question
        pending = await get_sqlite_session_store().get_reading_quiz_pending(book['material_id'], point['start'])
        assert pending[question['id']]['knowledge_point_id'] == point['id']
        payload = QuizAnswersPayload(locator=point['start'], submission_id='same-submit', answers=[{'question_id':question['id'], 'selected_index':1}])
        first = await submit_quiz_answers(book['material_id'], payload)
        second = await submit_quiz_answers(book['material_id'], payload)
        assert first['answers'][0]['is_correct'] and second['answers'][0]['is_correct']
        progress = LearningStore().load(book['path_id'])
        evidence = [e for e in progress.learning_evidence if e.knowledge_point_id == point['id']]
        assert len(evidence) == 1
        assert point['id'] in progress.repetition_states
        assert not progress.qualitative_mastery.get(point['id'], False)
    asyncio.run(scenario())


def test_same_named_points_in_different_books_stay_separate(native_book, tmp_path):
    second = tmp_path / 'other.md'
    second.write_text('# 中序遍历\n\n这是另一份资料，讨论线索二叉树。', encoding='utf-8')
    manifest = ReadingStore().ingest(second)
    book2 = service.bind_material(manifest.material_id)
    assert native_book['path_id'] != book2['path_id']
    assert not ({p['id'] for p in native_book['objectives']} & {p['id'] for p in book2['objectives']})


def test_native_mastery_tools_bind_same_textbook_server_side(native_book):
    from deeptutor.capabilities.mastery.loop import MasteryLoopCapability
    from deeptutor.capabilities.reading.tools import ReadMaterialTool
    from deeptutor.core.context import UnifiedContext
    context = UnifiedContext(user_message='读取本节', metadata={'mastery_mode':True, 'mastery_path_id':native_book['path_id']})
    point = native_book['objectives'][-1]
    kwargs = MasteryLoopCapability().augment_kwargs('read_material',
        {'locators':str(point['start']), '_material_id':'another-book'}, context)
    assert kwargs['_material_id'] == native_book['material_id']
    result = asyncio.run(ReadMaterialTool().execute(**kwargs))
    assert result.success and '中序遍历' in result.content


def test_reader_reuses_only_same_book_notes(native_book, tmp_path):
    book = native_book
    point = book['objectives'][-1]
    saved = notebook_manager.add_record([book['notebook_id']], 'reading', '本节理解', '摘录', '中序顺序',
        summary='我的理解', metadata={'locator':point['start']})
    refs = service.reading_note_references(book['material_id'], point['start'])
    assert refs and refs[0]['notebook_id'] == book['notebook_id']
    assert refs[0]['record_ids']
    assert service.reading_note_references('unknown-material', 1) == []


def test_visualization_note_retains_reading_objective(native_book):
    book = native_book
    metadata = service.enrich_note([book['notebook_id']], {'source':'immersive_reading', 'capability':'visualize'})
    assert metadata['bookmind']['knowledge_point_id'] == service.objective_at(service.find_book(id=book['id']), metadata['bookmind']['locator'])['id']


def test_binding_recovers_without_replacing_native_notebook(native_book):
    book = native_book
    with service.connection() as conn:
        conn.execute('DELETE FROM textbooks WHERE id=?', (book['id'],))
    recovered = service.bind_material(book['material_id'])
    assert recovered['path_id'] == book['path_id']
    assert recovered['workspace_id'] == book['workspace_id']
    assert recovered['notebook_id'] == book['notebook_id']


async def _practice_turn(book):
    from deeptutor.bookmind import practice
    from deeptutor.services.session import get_sqlite_session_store
    sid = await practice.open_session(book)
    store = get_sqlite_session_store()
    turn = await store.create_turn(sid, 'deep_question')
    tid = turn.get('turn_id') or turn['id']
    point = book['objectives'][-1]
    payload = {'config': {'num_questions': 2, 'difficulty': 'hard',
        'question_types': ['choice', 'short_answer'], 'per_type_counts': {'choice': 1, 'short_answer': 1},
        'bookmind_objective_id': point['id'], 'bookmind_start': point['start'], 'bookmind_end': point['end']}}
    practice.prepare_turn(sid, tid, 'deep_question', payload)
    questions = [
        {'question_id': 'q_1', 'question': '中序遍历何时访问根？', 'question_type': 'choice', 'options': {'A': '最先', 'B': '左右之间'}, 'correct_answer': 'B', 'explanation': '左根右', 'difficulty': 'hard'},
        {'question_id': 'q_2', 'question': '解释递归过程', 'question_type': 'short_answer', 'correct_answer': '先左子树，再根，最后右子树'}]
    for q in questions:
        await store.append_turn_event(tid, {'type': 'content', 'metadata': {'call_kind': 'quiz_question_emitted', 'qa_pair': q}})
    await store.update_turn_status(tid, 'completed')
    return sid, tid, payload, questions


def test_full_practice_uses_native_sources_and_preserves_mix(native_book):
    from deeptutor.bookmind import practice
    from deeptutor.reading.references import resolve_reading_sources
    async def scenario():
        sid, tid, payload, questions = await _practice_turn(native_book)
        assert await practice.open_session(native_book) == sid
        references = payload['reading_references']
        assert references[0]['material_id'] == native_book['material_id']
        assert '中序遍历' in resolve_reading_sources(references)[0].full_text
        snapshot = await practice.snapshot(native_book)
        assert snapshot['turn_id'] == tid and len(snapshot['questions']) == 2
        assert snapshot['config']['difficulty'] == 'hard'
        assert snapshot['config']['per_type_counts'] == {'choice': 1, 'short_answer': 1}
        from deeptutor.runtime.request_contracts import validate_deep_question_request_config
        validated = validate_deep_question_request_config(payload['config'])
        assert validated.bookmind_objective_id == native_book['objectives'][-1]['id']
    asyncio.run(scenario())


@pytest.mark.parametrize('override', [
    {'num_questions': 0}, {'num_questions': 51}, {'difficulty': 'impossible'},
    {'question_types': ['unknown']}, {'per_type_counts': {'choice': 2, 'short_answer': 1}},
    {'bookmind_objective_id': 'another-book-point'}, {'bookmind_end': 99999},
])
def test_full_practice_rejects_bad_config_and_source(native_book, override):
    from deeptutor.bookmind import practice
    async def scenario():
        sid, _, payload, _ = await _practice_turn(native_book)
        payload['config'].update(override)
        with pytest.raises(ValueError):
            practice.prepare_turn(sid, 'invalid-turn', 'deep_question', payload)
        assert practice.turn_scope(sid, 'invalid-turn') is None
    asyncio.run(scenario())


def test_full_practice_trusted_grades_share_evidence_without_duplicates(native_book):
    from deeptutor.bookmind import practice
    from deeptutor.api.routers.question_notebook import UpsertEntryRequest, upsert_single_entry
    async def scenario():
        sid, tid, _, questions = await _practice_turn(native_book)
        # Even a forged key, client grade and foreign objective are ignored.
        payload = UpsertEntryRequest(session_id=sid, turn_id=tid, question_id='q_1',
            question='forged question', correct_answer='A', user_answer='A', is_correct=True,
            mastery_path_id='foreign-path', knowledge_point_id='foreign-point')
        first = await upsert_single_entry(payload)
        second = await practice.record_answer(sid, tid, payload.model_dump())
        from deeptutor.api.routers.sessions import QuizResultsRequest, record_quiz_results
        bulk = QuizResultsRequest(turn_id=tid, answers=[payload.model_dump()])
        await record_quiz_results(sid, bulk)
        await record_quiz_results(sid, bulk)
        assert first['id'] == second['id'] and second['result'] == 'incorrect'
        assert second['correct_answer'] == 'B' and second['question'] == questions[0]['question']
        assert second['mastery_path_id'] == native_book['path_id']
        progress = LearningStore().load(native_book['path_id'])
        assert len(progress.learning_evidence) == 1
        assert progress.review_queue
        subjective = await practice.record_answer(sid, tid, {'question_id': 'q_2', 'user_answer': '我的解释', 'is_correct': True})
        assert subjective['result'] == 'ungraded'
        assert len(LearningStore().load(native_book['path_id']).learning_evidence) == 1
        with pytest.raises(ValueError):
            await practice.record_answer(sid, tid, {'question_id': 'other-q', 'user_answer': 'B'})
        with pytest.raises(ValueError):
            await practice.record_answer('other-session', tid, {'question_id': 'q_1', 'user_answer': 'B'})
    asyncio.run(scenario())


def test_textbook_mistake_reattempt_updates_same_point_once(native_book):
    from deeptutor.bookmind import practice
    from deeptutor.api.routers.practice import review, ReviewRequest
    async def scenario():
        sid, tid, _, _ = await _practice_turn(native_book)
        entry = await practice.record_answer(sid, tid, {'question_id': 'q_1', 'user_answer': 'A'})
        request = ReviewRequest(answer='B', request_id='bookmind-reattempt-001', version=0, rating='good')
        first = await review(entry['id'], request)
        second = await review(entry['id'], request)
        assert first == second
        progress = LearningStore().load(native_book['path_id'])
        assert len(progress.learning_evidence) == 2
        assert progress.learning_evidence[-1].result == 'correct'
    asyncio.run(scenario())


def _ready_generated_book():
    from deeptutor.book.engine import BookEngine
    from deeptutor.book.storage import BookStorage
    from deeptutor.book.models import Book, BookInputs, BookStatus, Chapter, Spine, Page, PageStatus, Block, BlockStatus, BlockType
    engine = BookEngine(storage=BookStorage())
    book = Book(title='从目标理解二叉树', language='zh', status=BookStatus.READY)
    chapter = Chapter(title='二叉树与中序遍历', learning_objectives=['理解左右孩子', '能解释左根右的访问顺序'])
    page = Page(book_id=book.id, chapter_id=chapter.id, title=chapter.title, status=PageStatus.READY,
        blocks=[Block(type=BlockType.SECTION, status=BlockStatus.READY, payload={
            'intro': '二叉树每个结点至多有两个孩子。',
            'subsections': [{'heading': '中序遍历', 'body': '先访问左子树，再访问根，最后访问右子树。'}],
            'key_takeaway': '二叉树的左右位置有区别。'})])
    chapter.page_ids = [page.id]
    spine = Spine(book_id=book.id, chapters=[chapter])
    engine.storage.save_book(book)
    engine.storage.save_spine(spine)
    engine.storage.save_page(page)
    engine.storage.save_inputs(book.id, BookInputs(user_intent='我想学会二叉树和中序遍历', language='zh'))
    return engine, book, page


def test_generated_books_join_same_learning_surfaces_once(native_book):
    from deeptutor.bookmind.generated import import_generated_book
    engine, source, page = _ready_generated_book()
    first = import_generated_book(source.id, engine)
    assert first['generated_book_id'] == source.id
    assert first['learning_goal'] == '我想学会二叉树和中序遍历'
    assert first['objectives']
    assert set(first['links']) == {'learn', 'guide', 'practice', 'notes'}
    assert '二叉树' in service.reading_sources(LearningStore().get_topic(first['path_id']).sources[0], 20000)[0].full_text
    notebook_manager.add_record([first['notebook_id']], 'reading', '我的理解', '摘录', '二叉树左右孩子有区别')
    second = import_generated_book(source.id, engine)
    assert second['id'] == first['id'] and second['notebook_id'] == first['notebook_id']
    assert second['note_count'] == 1


@pytest.mark.parametrize('invalid', ['draft', 'partial', 'missing_page', 'unfinished_block'])
def test_generated_books_require_complete_native_content(native_book, invalid):
    from deeptutor.bookmind.generated import import_generated_book
    from deeptutor.book.models import BookStatus, PageStatus, BlockStatus
    engine, book, page = _ready_generated_book()
    if invalid == 'draft':
        book.status = BookStatus.DRAFT
        engine.storage.save_book(book)
    elif invalid == 'partial':
        page.status = PageStatus.PARTIAL
        engine.storage.save_page(page)
    elif invalid == 'missing_page':
        spine = engine.load_spine(book.id)
        spine.chapters[0].page_ids.append('missing-page')
        engine.storage.save_spine(spine)
    else:
        page.blocks[0].status = BlockStatus.ERROR
        engine.storage.save_page(page)
    with pytest.raises(ValueError):
        import_generated_book(book.id, engine)
    assert len(service.books()) == 1


def test_separate_generated_books_keep_separate_learning_records(native_book):
    from deeptutor.bookmind.generated import import_generated_book
    engine, source, _ = _ready_generated_book()
    first = import_generated_book(source.id, engine)
    engine2, source2, _ = _ready_generated_book()
    second = import_generated_book(source2.id, engine2)
    assert first['material_id'] != second['material_id']
    assert first['path_id'] != second['path_id'] and first['notebook_id'] != second['notebook_id']
