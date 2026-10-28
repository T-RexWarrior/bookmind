"""Read v2 without modifying it; ingest its files and notes into native stores.

Scores are deliberately not mapped to newly created objectives by a name guess.
The old application and all its original records remain available in v2.
"""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bookmind.textbook.service import bind_material
from bookmind.reading import ReadingCatalogStore, ReadingPosition, ReadingStore
from bookmind.services.notebook import notebook_manager

legacy = ROOT.parent / 'BookMind-v2' / 'data'
conn = sqlite3.connect((legacy / 'studytutor.db').as_uri() + '?mode=ro', uri=True)
conn.row_factory = sqlite3.Row
reading, catalog = ReadingStore(), ReadingCatalogStore()
out = ROOT / 'data' / 'migration'
out.mkdir(parents=True, exist_ok=True)
report = []
for material in conn.execute('SELECT * FROM materials ORDER BY created'):
    if material['filename']:
        source = legacy / 'uploads' / material['filename']
        filename = material['title'] + '.pdf'
    else:
        source = out / (material['id'] + '.md')
        pages = conn.execute('SELECT * FROM pages WHERE material_id=? ORDER BY number', (material['id'],)).fetchall()
        source.write_text('\n\n'.join(row['text'] for row in pages), encoding='utf-8')
        filename = material['title'] + '.md'
    manifest = reading.ingest(source, filename=filename)
    catalog.register_manifest(manifest)
    book = bind_material(manifest.material_id)
    position = conn.execute('SELECT focus_page FROM spaces WHERE id=?', (material['space_id'],)).fetchone()
    if position and material['kind'] == 'pdf':
        reading.save_position(manifest.material_id, ReadingPosition(locator=min(position['focus_page'], manifest.unit_count)))
    notebook = notebook_manager.get_notebook(book['notebook_id'])
    imported = {r.get('metadata', {}).get('legacy_note_id') for r in notebook['records']}
    count = 0
    for note in conn.execute('SELECT * FROM notes WHERE space_id=?', (material['space_id'],)):
        if note['id'] in imported:
            continue
        ref = json.loads(note['source'])
        metadata = {'legacy_note_id': note['id'], 'legacy_source': ref, 'source': 'reading'}
        if material['kind'] == 'pdf' and isinstance(ref.get('page'), int):
            metadata['locator'] = ref['page']
        notebook_manager.add_record(notebook_ids=[book['notebook_id']], record_type='reading',
            title=note['title'], user_query='从 BookMind v2 迁移的学习笔记', output=note['content'],
            summary=note['title'], metadata=metadata)
        count += 1
    report.append({'legacy_material_id': material['id'], 'book_id': book['id'],
        'material_id': manifest.material_id, 'units': manifest.unit_count, 'notes_imported': count})
(out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'materials': len(report), 'notes_imported': sum(r['notes_imported'] for r in report)}))
