"""Textbook scoping for the native deep_question pipeline and QuizViewer.

Only bindings and source snapshots live here. Native sessions own generation;
Question Notebook and the shared assessment adapter own answers and evidence.
"""
from __future__ import annotations

import asyncio
import json
import time

from pydantic import BaseModel, Field

from bookmind.reading import ReadingStore
from bookmind.services.session import get_sqlite_session_store
from . import service
from .repository import connection

_session_lock = asyncio.Lock()
TYPES = {'choice', 'concept', 'fill_in_blank', 'short_answer', 'written', 'coding'}


def _tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS practice_sessions (
        book_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS practice_turns (
        turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, book_id TEXT NOT NULL,
        objective_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
        revision INTEGER NOT NULL, config_json TEXT NOT NULL, created_at REAL NOT NULL)''')


def binding(session_id):
    with connection() as conn:
        _tables(conn)
        row = conn.execute('SELECT book_id FROM practice_sessions WHERE session_id=?', (session_id,)).fetchone()
        return service.find_book(id=row['book_id']) if row else None


async def open_session(book):
    async with _session_lock:
        store = get_sqlite_session_store()
        with connection() as conn:
            _tables(conn)
            row = conn.execute('SELECT session_id FROM practice_sessions WHERE book_id=?', (book['id'],)).fetchone()
        if row and await store.get_session(row['session_id']):
            return row['session_id']
        session = await store.create_session(title=f"练习巩固 · {book['title']}")
        sid = session.get('session_id') or session['id']
        with connection() as conn:
            conn.execute('INSERT INTO practice_sessions VALUES(?,?) ON CONFLICT(book_id) DO UPDATE SET session_id=excluded.session_id', (book['id'], sid))
        return sid


class PracticeConfig(BaseModel):
    mode: str = 'custom'
    num_questions: int = Field(default=3, ge=1, le=50, strict=True)
    difficulty: str = ''
    question_types: list[str] = Field(default_factory=list)
    per_type_counts: dict[str, int] = Field(default_factory=dict)
    bookmind_objective_id: str
    bookmind_start: int = Field(ge=1, strict=True)
    bookmind_end: int = Field(ge=1, strict=True)


def prepare_turn(session_id, turn_id, capability, payload):
    book = binding(session_id)
    if not book:
        return
    if capability != 'deep_question' or payload.get('workspace_mode') not in (None, '', 'chat'):
        raise ValueError('教材练习会话只能用于专项出题')
    config = PracticeConfig.model_validate(payload.get('config') or {})
    if config.mode != 'custom' or config.difficulty not in {'', 'auto', 'easy', 'medium', 'hard'}:
        raise ValueError('请选择有效的出题模式与难度')
    selected = set(config.question_types)
    if not selected <= TYPES or len(selected) != len(config.question_types):
        raise ValueError('题型配置无效')
    counts = config.per_type_counts
    if counts and (set(counts) != selected or any(n < 1 for n in counts.values()) or sum(counts.values()) != config.num_questions):
        raise ValueError('各题型数量之和必须等于总题数')
    _, point = service.verified_practice_objective(book['material_id'], config.bookmind_objective_id, config.bookmind_start)
    start, end = config.bookmind_start, config.bookmind_end
    if not start <= end <= point['end'] or end - start >= 24:
        raise ValueError('出题范围须在当前知识点内，每次最多选取 24 个原文单元')
    manifest = ReadingStore().manifest(book['material_id'])
    previous = turn_scope(session_id, turn_id)
    if previous and (previous['revision'] != manifest.revision or json.loads(previous['config_json']) != config.model_dump()):
        raise ValueError('本轮出题的教材版本或范围已变化，请开始新的练习')
    # Resolve actual source text with the native attachment/reference intake.
    # No embedding provider and no client-supplied source text are required.
    payload['reading_references'] = [{'material_id': book['material_id'], 'revision': manifest.revision, 'locators': list(range(start, end + 1))}]
    payload['knowledge_bases'] = []
    with connection() as conn:
        _tables(conn)
        conn.execute('INSERT OR IGNORE INTO practice_turns VALUES(?,?,?,?,?,?,?,?,?)',
            (turn_id, session_id, book['id'], point['id'], start, end, manifest.revision, json.dumps(config.model_dump()), time.time()))


def turn_scope(session_id, turn_id):
    with connection() as conn:
        _tables(conn)
        row = conn.execute('SELECT * FROM practice_turns WHERE session_id=? AND turn_id=?', (session_id, turn_id)).fetchone()
        return dict(row) if row else None


def extract_questions(events):
    questions = {}
    for event in events:
        meta = event.get('metadata') or {}
        if event.get('type') == 'content' and meta.get('call_kind') == 'quiz_question_emitted':
            qa = meta.get('qa_pair') or {}
            if qa.get('question_id') and qa.get('question'):
                questions[str(qa['question_id'])] = qa
        if event.get('type') == 'result':
            for item in (meta.get('summary') or {}).get('results') or []:
                qa = item.get('qa_pair') or item
                if qa.get('question_id') and qa.get('question'):
                    questions[str(qa['question_id'])] = qa
    return list(questions.values())


async def questions_for_turn(session_id, turn_id):
    if not turn_scope(session_id, turn_id):
        raise ValueError('练习记录不属于当前教材会话')
    store = get_sqlite_session_store()
    turn = await store.get_turn(turn_id)
    if not turn or turn.get('session_id') != session_id or turn.get('status') != 'completed':
        raise ValueError('本次练习尚未生成完成')
    return extract_questions(await store.get_turn_events(turn_id))


async def snapshot(book):
    sid = await open_session(book)
    with connection() as conn:
        rows = [dict(row) for row in conn.execute('SELECT * FROM practice_turns WHERE book_id=? AND session_id=? ORDER BY created_at DESC LIMIT 20', (book['id'], sid))]
    store = get_sqlite_session_store()
    for row in rows:
        turn = await store.get_turn(row['turn_id'])
        if turn and turn.get('status') == 'completed':
            questions = await questions_for_turn(sid, row['turn_id'])
            if questions:
                return {'session_id': sid, 'turn_id': row['turn_id'], 'questions': questions,
                        'objective_id': row['objective_id'], 'start': row['start'], 'end': row['end'],
                        'config': json.loads(row['config_json'])}
    return {'session_id': sid, 'turn_id': None, 'questions': []}


def grade(qa, answer):
    """Only objective answers get deterministic grades. Prose remains ungraded."""
    kind, expected = qa.get('question_type'), str(qa.get('correct_answer') or '').strip()
    actual = answer.strip()
    if not expected or not actual:
        return None
    if kind in {'choice', 'multiple_choice', 'mcq'}:
        from bookmind.services.practice.answers import check_answer
        return check_answer({**qa, 'question_type': 'single_choice'}, actual)
    if kind in {'concept', 'true_false'}:
        def tf(value):
            value = value.strip().lower().rstrip('。.!！')
            return {'true': True, 't': True, '正确': True, '对': True, '是': True,
                    'false': False, 'f': False, '错误': False, '错': False, '否': False}.get(value)
        expected_bool, actual_bool = tf(expected), tf(actual)
        return None if expected_bool is None or actual_bool is None else expected_bool == actual_bool
    if kind == 'fill_in_blank':
        return actual.casefold() == expected.casefold()
    return None


async def record_answer(session_id, turn_id, item, *, attempt_id=''):
    """Ignore client answer keys/links/grades; load the generated question."""
    scope = turn_scope(session_id, turn_id)
    if not scope:
        raise ValueError('找不到当前教材的出题记录')
    book = service.find_book(id=scope['book_id'])
    point = next((p for p in service.objectives(book) if p['id'] == scope['objective_id']), None)
    questions = await questions_for_turn(session_id, turn_id)
    qa = next((q for q in questions if str(q['question_id']) == str(item.get('question_id'))), None)
    if not qa or not point:
        raise ValueError('题目或知识点不属于本次练习')
    answer = str(item.get('user_answer') or '')
    correct = grade(qa, answer)
    from bookmind.learning.assessment import AssessmentRecord, record_assessment
    result = 'ungraded' if correct is None else 'correct' if correct else 'incorrect'
    record = AssessmentRecord(session_id=session_id, turn_id=turn_id,
        question_id=str(qa['question_id']), source='deep_question', assessment_type='quiz',
        question=str(qa['question']), question_type=str(qa.get('question_type') or ''),
        options=qa.get('options') or {}, correct_answer=str(qa.get('correct_answer') or ''),
        explanation=str(qa.get('explanation') or ''), difficulty=str(qa.get('difficulty') or ''),
        user_answer=answer, result=result, is_correct=correct,
        material_id=book['material_id'], material_title=book['title'],
        section_id=str(scope['start']), section_title=point['name'],
        mastery_path_id=book['path_id'], knowledge_point_id=point['id'], attempt_id=attempt_id)
    outcome = await record_assessment(record)
    return await get_sqlite_session_store().get_notebook_entry(outcome.entry_id)
