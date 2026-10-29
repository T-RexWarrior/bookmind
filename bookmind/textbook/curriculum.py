"""Build stable knowledge-point identities and source ranges from a material outline."""
from __future__ import annotations

import hashlib
import re

from bookmind.learning.models import (
    KnowledgePoint, KnowledgeType, LearningModule, TopicSource, TopicSourceKind,
)

def build_curriculum(material_id, store, path_id):
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
        from bookmind.reading.models import OutlineEntry
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
                               module_id=module.id, topic_source_ids=[source_id],
                               planning_status='outline', source_section_id=kp_id)
        module.knowledge_points.append(point)
        ranges.append({'knowledge_point_id': kp_id, 'module_id': module.id, 'name': point.name,
                       'start': start, 'end': max(start, end), 'level': heading.level})
        sources.append(TopicSource(id=source_id, kind=TopicSourceKind.READING, source_id=material_id,
            label=f'{heading.title} · {start}–{max(start, end)}', position=index,
            metadata={'locator_start': start, 'locator_end': max(start, end), 'knowledge_point_id': kp_id}))
    return modules, ranges, sources
