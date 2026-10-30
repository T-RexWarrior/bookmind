import asyncio

import pytest

from bookmind.learning.storage import LearningStore
from bookmind.reading import ReadingStore
from bookmind.services.notebook import notebook_manager
from bookmind.textbook import repository, service
from bookmind.textbook.router import get_book, list_books, remove_book, restore_book


def test_remove_and_restore_preserve_notes_source_and_learning(native_book):
    book = native_book
    notebook_manager.add_record([book['notebook_id']], 'reading', '笔记', '摘录', '左右孩子有序')
    before = LearningStore().load(book['path_id']).model_dump(mode='json')
    source = ReadingStore().manifest(book['material_id']).to_dict()
    asyncio.run(remove_book(book['id']))
    asyncio.run(remove_book(book['id']))  # Idempotent removal, no duplicate state.
    assert service.books() == []
    assert asyncio.run(list_books()) == []
    with pytest.raises(Exception) as failure:
        asyncio.run(get_book(book['id']))
    assert failure.value.status_code == 404
    removed = asyncio.run(list_books(include_removed=True))
    assert len(removed) == 1 and removed[0]['removed_at']
    assert 'objectives' not in removed[0]
    assert LearningStore().load(book['path_id']).model_dump(mode='json') == before
    assert ReadingStore().manifest(book['material_id']).to_dict() == source
    restored = asyncio.run(restore_book(book['id']))
    assert restored['notebook_id'] == book['notebook_id']
    assert restored['note_count'] == 1
    assert restored['removed_at'] is None


def test_reimport_restores_binding_and_does_not_duplicate_native_state(native_book):
    book = native_book
    service.remove_book(book['id'])
    restored = service.bind_material(book['material_id'])
    assert restored['id'] == book['id']
    assert restored['workspace_id'] == book['workspace_id']
    assert restored['path_id'] == book['path_id']
    assert restored['notebook_id'] == book['notebook_id']
    assert len(repository.books(include_removed=True)) == 1


def test_removal_does_not_release_practice_conversation_ownership(native_book):
    from bookmind.textbook.practice import open_session, binding
    from bookmind.textbook.session_scope import assert_session_surface
    sid = asyncio.run(open_session(native_book))
    service.remove_book(native_book['id'])
    assert binding(sid)['id'] == native_book['id']
    with pytest.raises(RuntimeError, match='专项练习'):
        assert_session_surface(sid, {}, 'chat', 'immersive_reading', {})


def test_removed_generated_book_stays_identifiable_and_can_be_restored(native_book):
    from tests.test_bookmind_native import _ready_generated_book
    from bookmind.textbook.generated import import_generated_book
    engine, source, _ = _ready_generated_book()
    book = import_generated_book(source.id, engine)
    service.remove_book(book['id'])
    removed = [b for b in asyncio.run(list_books(True)) if b.get('removed_at')]
    assert removed[0]['generated_book_id'] == source.id
    assert import_generated_book(source.id, engine)['id'] == book['id']
    assert len(service.books()) == 2


def test_bound_guidance_cannot_be_deleted_independently(native_book):
    from fastapi import HTTPException
    from bookmind.api.routers.mastery_path import delete_progress
    before = LearningStore().load(native_book['path_id']).model_dump()
    with pytest.raises(HTTPException) as error:
        asyncio.run(delete_progress(native_book['path_id']))
    assert error.value.status_code == 409
    assert LearningStore().load(native_book['path_id']).model_dump() == before
    service.remove_book(native_book['id'])
    with pytest.raises(HTTPException):
        asyncio.run(delete_progress(native_book['path_id']))


def test_bound_guidance_repairs_missing_route_with_stable_ids(native_book, monkeypatch):
    from bookmind.api.routers.mastery_path import get_topic
    monkeypatch.setenv('BOOKMIND_APP', '1')
    store = LearningStore()
    expected = {p['id'] for p in native_book['objectives']}
    store.delete(native_book['path_id'])
    topic = asyncio.run(get_topic(native_book['path_id']))
    assert {p['id'] for m in topic['map']['modules'] for p in m['knowledge_points']} == expected
    assert any(s.source_id == native_book['notebook_id'] for s in store.get_topic(native_book['path_id']).sources)
    assert store.load(native_book['path_id']).quiz_attempts == []
    version = store.load(native_book['path_id']).version
    service.bind_material(native_book['material_id'])
    assert store.load(native_book['path_id']).version == version


def test_clearing_generation_records_archives_sources_and_blocks_resume(native_book, monkeypatch):
    from tests.test_bookmind_native import _ready_generated_book
    from bookmind.textbook.router import ClearGeneratedRequest, clear_generated_records
    from types import SimpleNamespace
    from bookmind.book.models import BookStatus
    engine, source, page = _ready_generated_book()
    monkeypatch.setattr('bookmind.api.routers.book._resolve_book_or_404', lambda book_id, **kwargs: SimpleNamespace(engine=engine))
    async def scenario():
        result = await clear_generated_records(ClearGeneratedRequest(book_ids=[source.id]))
        assert result == {'removed': [source.id]}
        assert engine.load_book(source.id).status == BookStatus.ARCHIVED
        assert engine.load_page(source.id, page.id) is not None
        with pytest.raises(ValueError, match='deleted'):
            engine.storage.save_book(source)  # Late completion holding pre-deletion state.
        await engine._maybe_finalize_book(source.id)
        assert engine.load_book(source.id).status == BookStatus.ARCHIVED
        with pytest.raises(ValueError, match='deleted'):
            await engine.resume_book(book_id=source.id)
    asyncio.run(scenario())


def test_clearing_records_rejects_imported_materials_before_any_mutation(native_book, monkeypatch):
    from tests.test_bookmind_native import _ready_generated_book
    from bookmind.textbook.generated import import_generated_book
    from bookmind.textbook.router import ClearGeneratedRequest, clear_generated_records
    from types import SimpleNamespace
    from fastapi import HTTPException
    from bookmind.book.models import BookStatus
    engine, source, _ = _ready_generated_book()
    bound = import_generated_book(source.id, engine)
    other_engine, other, _ = _ready_generated_book()
    engines = {source.id: engine, other.id: other_engine}
    monkeypatch.setattr('bookmind.api.routers.book._resolve_book_or_404', lambda book_id, **kwargs: SimpleNamespace(engine=engines[book_id]))
    with pytest.raises(HTTPException) as error:
        asyncio.run(clear_generated_records(ClearGeneratedRequest(book_ids=[other.id, source.id])))
    assert error.value.status_code == 409
    assert other_engine.load_book(other.id).status == BookStatus.READY
    assert service.find_book(id=bound['id']) is not None
