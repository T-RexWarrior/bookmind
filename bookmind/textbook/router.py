from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from bookmind.services.text_model import TextModelSettings, save_text_settings, text_settings

from . import service

router = APIRouter(prefix='/api/bookmind', tags=['BookMind'])


class BindRequest(BaseModel):
    material_id: str


class PlanRequest(BaseModel):
    objective_id: str
    goal: str = Field(default='', max_length=1500)
    prior_knowledge: str = Field(default='', max_length=1000)


class AdoptPlanRequest(BaseModel):
    plan_id: str


@router.post('/books/{book_id}/learning-plan/preview')
async def preview_learning_plan(book_id: str, body: PlanRequest):
    from .planning import PlanningError, generate_plan
    book = await asyncio.to_thread(service.find_book, id=book_id)
    if not book:
        raise HTTPException(404, '教材不存在')
    try:
        return await generate_plan(book, body.objective_id, goal=body.goal, prior_knowledge=body.prior_knowledge)
    except PlanningError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, '学习目标生成失败，请检查文字模型配置后重试。') from exc


@router.post('/books/{book_id}/learning-plan/apply')
async def adopt_learning_plan(book_id: str, body: AdoptPlanRequest):
    from bookmind.learning.storage import PathLeaseConflictError
    from .planning import PlanningError, apply_plan
    book = await asyncio.to_thread(service.find_book, id=book_id)
    if not book:
        raise HTTPException(404, '教材不存在')
    try:
        return await asyncio.to_thread(apply_plan, book, body.plan_id)
    except PathLeaseConflictError as exc:
        raise HTTPException(409, '当前有导学正在运行，请结束本轮对话后采用学习目标。') from exc
    except PlanningError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/generated/{native_book_id}/import')
async def import_goal_material(native_book_id: str):
    from bookmind.api.routers.book import _resolve_book_or_404
    from .generated import import_generated_book
    resolved = _resolve_book_or_404(native_book_id)
    try:
        return await asyncio.to_thread(import_generated_book, native_book_id, resolved.engine)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/books')
async def list_books(include_removed: bool = False):
    def listing():
        return [({'id': b['id'], 'title': b['title'], 'removed_at': b['removed_at'],
                  'generated_book_id': (service.repository.generated_source(material_id=b['material_id']) or {}).get('native_book_id')}
                 if b.get('removed_at') else service.overview(b))
                for b in service.books(include_removed=include_removed)]
    return await asyncio.to_thread(listing)


@router.delete('/books/{book_id}')
async def remove_book(book_id: str):
    try:
        return await asyncio.to_thread(service.remove_book, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/books/{book_id}/restore')
async def restore_book(book_id: str):
    try:
        return await asyncio.to_thread(service.restore_book, book_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/books', status_code=201)
async def bind_book(body: BindRequest):
    try:
        return await asyncio.to_thread(service.bind_material, body.material_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get('/books/{book_id}')
async def get_book(book_id: str):
    book = await asyncio.to_thread(service.find_book, id=book_id)
    if not book:
        raise HTTPException(404, '教材不存在')
    return await asyncio.to_thread(service.overview, book)


@router.post('/books/{book_id}/practice/session')
async def practice_session(book_id: str):
    from .practice import snapshot
    book = await asyncio.to_thread(service.find_book, id=book_id)
    if not book:
        raise HTTPException(404, '教材不存在')
    return await snapshot(book)


@router.get('/settings')
async def get_settings():
    return await asyncio.to_thread(text_settings)


@router.put('/settings')
async def put_settings(body: TextModelSettings):
    try:
        return await asyncio.to_thread(save_text_settings, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/settings/test')
async def test_settings():
    from bookmind.services.llm import complete
    try:
        reply = await complete(prompt='请只回复：连接成功', max_tokens=64, temperature=0, max_retries=0)
        return {'ok': True, 'reply': reply}
    except Exception as exc:
        raise HTTPException(502, f'连接测试失败（{type(exc).__name__}），请检查 API 配置。') from exc
