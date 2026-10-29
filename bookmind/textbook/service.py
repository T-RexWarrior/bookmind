"""Coordinate a material, reading workspace, learning path and notebook.

This application service joins native domain stores without owning their state.
SQL persistence and outline-to-curriculum mapping live in dedicated modules.
"""
from __future__ import annotations

import json
import time

from bookmind.learning.models import TopicMetadata, TopicSource, TopicSourceKind
from bookmind.learning.service import LearningService
from bookmind.learning.storage import LearningStore
from bookmind.reading import ReadingCatalogStore, ReadingStore

from . import repository
from .curriculum import build_curriculum
from .repository import books as books, find_book as find_book

def bind_material(material_id):
    """Idempotently create the three native aggregates for one existing material."""
    from bookmind.services.notebook import notebook_manager
    with repository.binding_lock:
        existing = find_book(material_id=material_id)
        if existing:
            return overview(existing)
        reading, catalog, learning = ReadingStore(), ReadingCatalogStore(), LearningStore()
        manifest = reading.manifest(material_id)
        if catalog.get_material(material_id) is None:
            catalog.register_manifest(manifest)
        book_id, path_id = f'bm_{material_id}', f'bmp_{material_id}'
        title = manifest.title or manifest.filename
        modules, ranges, sources = build_curriculum(material_id, reading, path_id)
        # If a previous process stopped between creating the native aggregates
        # and committing their binding, reuse the notebook already on the path.
        prior_topic = learning.get_topic(path_id) if learning.exists(path_id) else None
        prior_notebook_id = next((source.source_id for source in prior_topic.sources
            if source.kind == TopicSourceKind.NOTEBOOK), '') if prior_topic else ''
        notebook = notebook_manager.get_notebook(prior_notebook_id) if prior_notebook_id else None
        if notebook is None:
            notebook = notebook_manager.create_notebook(title, description=f'BookMind textbook {book_id}', color='#155e52')
        sources.append(TopicSource(id=f'notes_{material_id}', kind=TopicSourceKind.NOTEBOOK,
            source_id=notebook['id'], label=f'学习者笔记 · {title}', position=len(sources)))
        if not learning.exists(path_id):
            LearningService(learning).create_topic(path_id, name=title, modules=modules,
                metadata=TopicMetadata(path_id=path_id, goal=f'理解并掌握《{title}》',
                    description='围绕教材原文与学习目的规划目标，阅读、AI 导学、练习和笔记共用这条学习路线。'),
                sources=sources)
        elif prior_topic and not any(source.kind == TopicSourceKind.NOTEBOOK and source.source_id == notebook['id'] for source in prior_topic.sources):
            prior_topic.sources.append(sources[-1])
            learning.put_topic(prior_topic.metadata, prior_topic.sources)
        workspace_id = f'rw_bm_{material_id}'
        workspace = catalog.get_workspace(workspace_id)
        if workspace is None:
            workspace = catalog.create_workspace(title, [material_id], workspace_id=workspace_id)
        repository.insert_book(
            (book_id, material_id, workspace.workspace_id, path_id, notebook['id'], title,
             json.dumps(ranges, ensure_ascii=False), time.time()))
        return overview(find_book(id=book_id))


def objectives(book):
    progress = LearningStore().load(book['path_id'])
    if progress is None:
        return []
    ranges = {item['knowledge_point_id']: item for item in json.loads(book['ranges_json'])}
    # Edited objectives inherit only explicit source references; do not guess by name.
    topic = LearningStore().get_topic(book['path_id'], progress=progress)
    sources = {source.id: source for source in topic.sources} if topic else {}
    result = []
    for module in progress.modules:
        for point in module.knowledge_points:
            span = ranges.get(point.source_section_id or point.id)
            if not span or point.planning_status == 'planned':
                ref = next((sources[s] for s in point.topic_source_ids if s in sources and sources[s].kind == TopicSourceKind.READING), None)
                if ref:
                    span = {'start': ref.metadata.get('locator_start', 1), 'end': ref.metadata.get('locator_end', 1), 'level': 2}
            evidence = [e for e in progress.learning_evidence if e.knowledge_point_id == point.id]
            attempts = [a for a in progress.quiz_attempts if a.knowledge_point_id == point.id and not a.voided]
            state = progress.repetition_states.get(point.id)
            result.append({'id': point.id, 'name': point.name, 'module_id': module.id, 'module_name': module.name,
                'start': span['start'] if span else None, 'end': span['end'] if span else None,
                'level': span.get('level', 2) if span else 2,
                'type': point.type.value, 'planning_status': point.planning_status,
                'mastery_requirement': point.mastery_requirement,
                'source_section_id': point.source_section_id or point.id,
                'evidence_count': len(evidence), 'attempt_count': len(attempts),
                'mastery': progress.mastery_levels.get(point.id, 0),
                'next_review': state.next_review_at if state and hasattr(state, 'next_review_at') else None})
    return result


def objective_at(book, locator):
    candidates = [p for p in objectives(book) if p['start'] is not None and p['start'] <= locator <= p['end']]
    return max(candidates, key=lambda p: (p['start'], p['level']), default=None)


def verified_practice_objective(material_id, objective_id, locator):
    """Only associate an assessment with an actual source span in this book."""
    book = find_book(material_id=material_id)
    point = next((p for p in objectives(book) if p['id'] == objective_id), None) if book else None
    if not point or point['start'] is None or not point['start'] <= locator <= point['end']:
        raise ValueError('所选知识点与教材原文范围不匹配，请重新选择。')
    return book, point


def overview(book):
    if not book:
        raise ValueError('Textbook not found')
    from bookmind.services.notebook import notebook_manager
    result = {key: value for key, value in book.items() if key != 'ranges_json'}
    generated = repository.generated_source(material_id=book['material_id'])
    if generated:
        result['generated_book_id'] = generated['native_book_id']
        result['learning_goal'] = generated['goal']
    result['objectives'] = objectives(book)
    result['sections'] = json.loads(book['ranges_json'])
    notebook = notebook_manager.get_notebook(book['notebook_id']) or {}
    result['note_count'] = len(notebook.get('records', []))
    progress = LearningStore().load(book['path_id'])
    module = next((m for m in progress.modules if m.id == progress.current_module_id), None) if progress else None
    result['learning_plans'] = progress.curriculum_plans if progress else {}
    result['current_objective_id'] = (module.knowledge_points[progress.current_kp_index].id
        if module and 0 <= progress.current_kp_index < len(module.knowledge_points) else '')
    result['locator'] = ReadingStore().position(book['material_id']).locator
    manifest = ReadingStore().manifest(book['material_id'])
    result['unit'] = manifest.unit
    result['unit_count'] = manifest.unit_count
    result['links'] = {'learn': f"/learning/reading/{book['workspace_id']}",
        'guide': f"/learning/mastery/{book['path_id']}",
        'practice': f"/learning/practice/{book['path_id']}", 'notes': f"/notebooks/{book['notebook_id']}"}
    return result


def enrich_note(notebook_ids, metadata):
    """Scope notes by the destination notebook and verified reading position."""
    metadata = dict(metadata or {})
    book = next((b for nid in notebook_ids if (b := find_book(notebook_id=nid))), None)
    if not book:
        return metadata
    metadata['bookmind'] = {'book_id': book['id'], 'material_id': book['material_id'], 'path_id': book['path_id']}
    locator = metadata.get('locator') or metadata.get('reading_locator')
    capability = metadata.get('capability') or metadata.get('source')
    if not locator and (capability in {'immersive_reading', 'reading'} or metadata.get('source') in {'immersive_reading', 'reading'}):
        locator = ReadingStore().position(book['material_id']).locator
    if isinstance(locator, int) and (point := objective_at(book, locator)):
        metadata['bookmind'].update(knowledge_point_id=point['id'], locator=locator)
    elif capability == 'mastery_path' or metadata.get('source') == 'mastery_path':
        current = overview(book)['current_objective_id']
        point = next((p for p in objectives(book) if p['id'] == current), None)
        if point:
            metadata['bookmind'].update(knowledge_point_id=point['id'], locator=point['start'])
    metadata['bookmind']['workspace_id'] = book['workspace_id']
    return metadata


def reading_sources(source, budget):
    """Load actual pages for the original Mastery read_source tool, with locators."""
    from bookmind.learning.topic_materials import TopicMaterial
    start = int(source.metadata.get('locator_start', 1))
    end = int(source.metadata.get('locator_end', start))
    rows, truncated = ReadingStore().read_units(source.source_id, range(start, end + 1), max_chars=min(24000, max(0, budget)))
    text = '\n\n'.join(f'[教材原文 · {source.source_id} · 第 {locator} 页/节]\n{value}' for locator, value in rows)
    return [TopicMaterial(sid=source.id, kind='reading', name=source.label,
        outline=f'原文位置 {start}–{end}', full_text=text,
        note='原文窗口已截断；请勿声称已读取整个小节。' if truncated else '')]


def reading_note_references(material_id, locator):
    from bookmind.services.notebook import notebook_manager
    book = find_book(material_id=material_id)
    if not book:
        return []
    point = objective_at(book, locator)
    records = (notebook_manager.get_notebook(book['notebook_id']) or {}).get('records', [])
    selected = [record['id'] for record in records
        if not record.get('metadata', {}).get('bookmind', {}).get('knowledge_point_id')
        or point and record.get('metadata', {}).get('bookmind', {}).get('knowledge_point_id') == point['id']]
    return [{'notebook_id':book['notebook_id'], 'record_ids':selected[-10:]}] if selected else []
