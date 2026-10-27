"""Join native Reading, Mastery Path and Notebook by durable textbook identity.

This store holds foreign keys and source ranges, never a second mastery score.
Reading/assessment state stays in DeepTutor's transactional LearningStore.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from deeptutor.learning.models import (
    KnowledgePoint, KnowledgeType, LearningModule, TopicMetadata, TopicSource, TopicSourceKind,
)
from deeptutor.learning.service import LearningService
from deeptutor.learning.storage import LearningStore
from deeptutor.reading import ReadingCatalogStore, ReadingStore

_lock = threading.RLock()


@contextmanager
def connection():
    root = ReadingStore().root
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / 'bookmind.sqlite', timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS textbooks (
            id TEXT PRIMARY KEY, material_id TEXT NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL UNIQUE, path_id TEXT NOT NULL UNIQUE,
            notebook_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            ranges_json TEXT NOT NULL, created_at REAL NOT NULL)''')
        yield conn


def books():
    with connection() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM textbooks ORDER BY created_at DESC')]


def find_book(**identity):
    allowed = {'id', 'material_id', 'workspace_id', 'path_id', 'notebook_id'}
    if len(identity) != 1 or not set(identity) <= allowed:
        raise ValueError('Use one textbook identity')
    key, value = next(iter(identity.items()))
    with connection() as conn:
        row = conn.execute(f'SELECT * FROM textbooks WHERE {key}=?', (value,)).fetchone()
        return dict(row) if row else None


def _curriculum(material_id, store, path_id):
    manifest = store.manifest(material_id)
    outline = sorted(store.outline(material_id), key=lambda row: row.locator)
    # PDF bookmarks often contain every example and bullet. Keep the full
    # outline in Reading; seed Mastery from chapters/sections, not 1,000 bullets.
    if len(outline) > 200:
        chapter = re.compile(r'^(?:第\s*[\d一二三四五六七八九十]+\s*章|Chapter\s+\d+)', re.IGNORECASE)
        chapter_roots = [row for row in outline if row.level == 1 and chapter.search(row.title.strip())]
        if len(chapter_roots) > 1:
            selected, inside = [], False
            for row in outline:
                if row.level == 1:
                    inside = bool(chapter.search(row.title.strip()))
                if inside and row.level <= 2:
                    selected.append(row)
            outline = selected
        else:
            outline = [row for row in outline if row.level <= 2]
    if not outline:
        from deeptutor.reading.models import OutlineEntry
        outline = [OutlineEntry(locator=1, title=manifest.title or manifest.filename, level=1)]
    modules, ranges, sources = [], [], []
    module = None
    for index, heading in enumerate(outline):
        start = max(1, min(heading.locator, manifest.unit_count))
        end = manifest.unit_count
        for following in outline[index + 1:]:
            if following.locator > start:
                end = following.locator - 1
                break
        if module is None or heading.level <= 1:
            module = LearningModule(id=f'{path_id}_m{len(modules)}', name=heading.title[:200], order=len(modules))
            modules.append(module)
        digest = hashlib.sha256(f'{material_id}:{start}:{heading.title}:{index}'.encode()).hexdigest()[:12]
        kp_id, source_id = f'kp_{digest}', f'rs_{digest}'
        point = KnowledgePoint(id=kp_id, name=heading.title[:200], type=KnowledgeType.CONCEPT,
                               module_id=module.id, topic_source_ids=[source_id])
        module.knowledge_points.append(point)
        ranges.append({'knowledge_point_id': kp_id, 'module_id': module.id, 'name': point.name,
                       'start': start, 'end': max(start, end), 'level': heading.level})
        sources.append(TopicSource(id=source_id, kind=TopicSourceKind.READING, source_id=material_id,
            label=f'{heading.title} · {start}–{max(start, end)}', position=index,
            metadata={'locator_start': start, 'locator_end': max(start, end), 'knowledge_point_id': kp_id}))
    return modules, ranges, sources


def bind_material(material_id):
    """Idempotently create the three native aggregates for one existing material."""
    from deeptutor.services.notebook import notebook_manager
    with _lock:
        existing = find_book(material_id=material_id)
        if existing:
            return overview(existing)
        reading, catalog, learning = ReadingStore(), ReadingCatalogStore(), LearningStore()
        manifest = reading.manifest(material_id)
        if catalog.get_material(material_id) is None:
            catalog.register_manifest(manifest)
        book_id, path_id = f'bm_{material_id}', f'bmp_{material_id}'
        title = manifest.title or manifest.filename
        modules, ranges, sources = _curriculum(material_id, reading, path_id)
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
                    description='根据教材目录初始化；阅读、练习和笔记共用本路线。可使用原版路线编辑器细化学习目标。'),
                sources=sources)
        elif prior_topic and not any(source.kind == TopicSourceKind.NOTEBOOK and source.source_id == notebook['id'] for source in prior_topic.sources):
            prior_topic.sources.append(sources[-1])
            learning.put_topic(prior_topic.metadata, prior_topic.sources)
        workspace_id = f'rw_bm_{material_id}'
        workspace = catalog.get_workspace(workspace_id)
        if workspace is None:
            workspace = catalog.create_workspace(title, [material_id], workspace_id=workspace_id)
        with connection() as conn:
            conn.execute('INSERT INTO textbooks VALUES(?,?,?,?,?,?,?,?)',
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
            span = ranges.get(point.id)
            if not span:
                ref = next((sources[s] for s in point.topic_source_ids if s in sources and sources[s].kind == TopicSourceKind.READING), None)
                if ref:
                    span = {'start': ref.metadata.get('locator_start', 1), 'end': ref.metadata.get('locator_end', 1), 'level': 2}
            evidence = [e for e in progress.learning_evidence if e.knowledge_point_id == point.id]
            attempts = [a for a in progress.quiz_attempts if a.knowledge_point_id == point.id and not a.voided]
            state = progress.repetition_states.get(point.id)
            result.append({'id': point.id, 'name': point.name, 'module_id': module.id, 'module_name': module.name,
                'start': span['start'] if span else None, 'end': span['end'] if span else None,
                'level': span.get('level', 2) if span else 2,
                'evidence_count': len(evidence), 'attempt_count': len(attempts),
                'mastery': progress.mastery_levels.get(point.id, 0),
                'next_review': state.next_review_at if state and hasattr(state, 'next_review_at') else None})
    return result


def objective_at(book, locator):
    candidates = [p for p in objectives(book) if p['start'] is not None and p['start'] <= locator <= p['end']]
    return max(candidates, key=lambda p: (p['start'], p['level']), default=None)


def overview(book):
    if not book:
        raise ValueError('Textbook not found')
    from deeptutor.services.notebook import notebook_manager
    result = {key: value for key, value in book.items() if key != 'ranges_json'}
    result['objectives'] = objectives(book)
    notebook = notebook_manager.get_notebook(book['notebook_id']) or {}
    result['note_count'] = len(notebook.get('records', []))
    progress = LearningStore().load(book['path_id'])
    module = next((m for m in progress.modules if m.id == progress.current_module_id), None) if progress else None
    result['current_objective_id'] = (module.knowledge_points[progress.current_kp_index].id
        if module and 0 <= progress.current_kp_index < len(module.knowledge_points) else '')
    result['locator'] = ReadingStore().position(book['material_id']).locator
    result['links'] = {'learn': f"/learning/reading/{book['workspace_id']}",
        'practice': f"/learning/mastery/{book['path_id']}", 'notes': f"/notebooks/{book['notebook_id']}"}
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
    from deeptutor.learning.topic_materials import TopicMaterial
    start = int(source.metadata.get('locator_start', 1))
    end = int(source.metadata.get('locator_end', start))
    rows, truncated = ReadingStore().read_units(source.source_id, range(start, end + 1), max_chars=min(24000, max(0, budget)))
    text = '\n\n'.join(f'[教材原文 · {source.source_id} · 第 {locator} 页/节]\n{value}' for locator, value in rows)
    return [TopicMaterial(sid=source.id, kind='reading', name=source.label,
        outline=f'原文位置 {start}–{end}', full_text=text,
        note='原文窗口已截断；请勿声称已读取整个小节。' if truncated else '')]


def reading_note_references(material_id, locator):
    from deeptutor.services.notebook import notebook_manager
    book = find_book(material_id=material_id)
    if not book:
        return []
    point = objective_at(book, locator)
    records = (notebook_manager.get_notebook(book['notebook_id']) or {}).get('records', [])
    selected = [record['id'] for record in records
        if not record.get('metadata', {}).get('bookmind', {}).get('knowledge_point_id')
        or point and record.get('metadata', {}).get('bookmind', {}).get('knowledge_point_id') == point['id']]
    return [{'notebook_id':book['notebook_id'], 'record_ids':selected[-10:]}] if selected else []
