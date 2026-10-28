from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from bookmind.services.text_model import TextModelSettings, save_text_settings, text_settings

from . import service

router = APIRouter(prefix='/api/bookmind', tags=['BookMind'])


class BindRequest(BaseModel):
    material_id: str


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
async def list_books():
    return await asyncio.to_thread(lambda: [service.overview(b) for b in service.books()])


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
