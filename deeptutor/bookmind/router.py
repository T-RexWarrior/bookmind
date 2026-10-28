from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

router = APIRouter(prefix='/api/bookmind', tags=['BookMind'])


class BindRequest(BaseModel):
    material_id: str


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


class TextModelSettings(BaseModel):
    base_url: str
    model: str
    api_key: str = ''


def text_settings():
    from deeptutor.services.config import get_model_catalog_service
    catalog_service = get_model_catalog_service()
    catalog = catalog_service.load()
    profile = catalog_service.get_active_profile(catalog, 'llm') or {}
    model = catalog_service.get_active_model(catalog, 'llm') or {}
    return {'base_url': profile.get('base_url', 'https://api.deepseek.com'),
            'model': model.get('model', ''), 'has_api_key': bool(profile.get('api_key'))}


@router.get('/settings')
async def get_settings():
    return await asyncio.to_thread(text_settings)


def save_text_settings(body):
    from deeptutor.services.config import get_model_catalog_service
    from deeptutor.services.llm.config import clear_llm_config_cache
    from urllib.parse import urlparse
    if urlparse(body.base_url).scheme not in {'http', 'https'} or not body.model.strip():
        raise ValueError('请填写有效的 API 地址和模型名称')
    catalog_service = get_model_catalog_service()
    def update(catalog):
        previous = catalog_service.get_active_profile(catalog, 'llm') or {}
        key = body.api_key.strip() or previous.get('api_key', '')
        catalog['services']['llm'] = {'active_profile_id': 'bookmind-text', 'active_model_id': 'bookmind-model',
            'profiles': [{'id': 'bookmind-text', 'name': 'BookMind 文字模型', 'provider': 'openai',
                'binding': 'openai', 'api_format': 'openai_chat_completions',
                'base_url': body.base_url.strip().rstrip('/'), 'api_key': key,
                'models': [{'id': 'bookmind-model', 'name': body.model.strip(), 'model': body.model.strip(),
                            'capabilities': {'function_calling': True}}]}]}
    catalog_service.update(update)
    clear_llm_config_cache()
    return text_settings()


@router.put('/settings')
async def put_settings(body: TextModelSettings):
    try:
        return await asyncio.to_thread(save_text_settings, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/settings/test')
async def test_settings():
    from deeptutor.services.llm import complete
    try:
        reply = await complete(prompt='请只回复：连接成功', max_tokens=64, temperature=0, max_retries=0)
        return {'ok': True, 'reply': reply}
    except Exception as exc:
        raise HTTPException(502, f'连接测试失败（{type(exc).__name__}），请检查 API 配置。') from exc
