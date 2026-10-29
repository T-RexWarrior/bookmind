"""Carry learner requirements and established examples through book writing.

This is editorial context, never retrieval evidence. No additional model calls.
"""
from __future__ import annotations

from contextvars import ContextVar
import re

from .models import BlockStatus, BlockType

active_writing_context: ContextVar[str] = ContextVar('book_writing_context', default='')


def book_writing_context(storage, book_id, chapter):
    inputs = storage.load_inputs(book_id)
    spine = storage.load_spine(book_id)
    book = storage.load_book(book_id)
    parts = [
        '[Learner requirements — honor these throughout the prose and code]',
        (inputs.user_intent if inputs else '')[:6000],
        '[Book outline — stay within the current chapter; do not reteach other chapters]',
        '\n'.join(f'- {c.title}: {c.summary}' for c in (spine.chapters if spine else []))[:5000],
        f'[Current chapter] {chapter.title}',
        f'[Reading depth] {getattr(book.depth, "value", book.depth)}' if book else '',
    ]
    # Carry the first completed preceding chapter's example into later chapters.
    for page in sorted(storage.list_pages(book_id), key=lambda p: p.order):
        if page.chapter_id == chapter.id:
            break
        if page.content_type.value == 'overview':
            continue
        excerpts = [_block_excerpt(b) for b in page.blocks if b.status == BlockStatus.READY
                    and b.type in {BlockType.SECTION, BlockType.CODE}]
        if any(excerpts):
            parts.extend(['[Established examples — reuse data, names, units and conventions]',
                          '\n'.join(excerpts)[:6500]])
            break
    parts.append('Write only the assigned focus. Reuse established examples and notation; '
                 'explain any necessary change. Do not invent source quotations or page numbers. '
                 'Make code examples self-contained: supply literal sample data or its creation '
                 'code instead of assuming a local file exists. Explain expected results and '
                 'boundary cases, not just a list of definitions.')
    return '\n\n'.join(p for p in parts if p)


def _block_excerpt(block):
    payload = block.payload or {}
    if block.type == BlockType.CODE:
        return str(payload.get('code', ''))[:3500]
    parts = [str(payload.get('intro', ''))]
    parts.extend(f"{s.get('heading', '')}: {s.get('body', '')}" for s in payload.get('subsections', [])
                 if isinstance(s, dict))
    return '\n'.join(parts)[:4000]


def block_writing_context(ctx):
    parts = [ctx.extra.get('writing_context', '')]
    parts.append('[Chapter block plan — each block has a distinct job]\n' + '\n'.join(
        f'{i+1}. {b.type.value}: {b.params.get("focus") or b.params.get("role") or b.title}'
        + (' ← WRITE THIS BLOCK' if b.id == ctx.block.id else '')
        for i, b in enumerate(ctx.page.blocks)))
    previous = []
    for block in ctx.page.blocks:
        if block.id == ctx.block.id:
            break
        if block.status == BlockStatus.READY and block.type in {BlockType.SECTION, BlockType.TEXT, BlockType.CODE}:
            previous.append(_block_excerpt(block) or str(block.payload.get('body', ''))[:1000])
    if previous:
        parts.append('[Already written in this chapter — continue it, avoid repeating it]\n' + '\n'.join(previous)[-6500:])
    parts.append(f'[Assigned focus] {ctx.block.params.get("focus") or ctx.block.params.get("role") or ctx.chapter.title}')
    return '\n\n'.join(p for p in parts if p)


def chapter_prose_budget(depth):
    return {'brief': 1000, 'standard': 2400, 'deep': 4000}.get(str(depth or 'standard'), 2400)


def allocate_prose_budget(blocks, depth, user_intent=''):
    """Share a chapter budget among its sections instead of repeating it."""
    sections = [b for b in blocks if b.type == BlockType.SECTION]
    if not sections:
        return
    total = chapter_prose_budget(depth)
    explicit = re.search(r'(?:每章|每一章|每个章节|each chapter)[^\n。.!]{0,24}?(\d{3,5})\s*(?:字|words)', user_intent, re.I)
    if explicit:
        total = max(300, min(12000, int(explicit[1])))
    weights = [max(1, int(b.params.get('target_words') or 1000)) for b in sections]
    for block, weight in zip(sections, weights):
        block.params['target_words'] = max(80, round(total * weight / sum(weights)))
        block.metadata['chapter_prose_budget'] = total
