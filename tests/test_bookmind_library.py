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
