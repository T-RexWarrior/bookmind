"""Text-model application service shared by HTTP settings and setup scripts.

Public responses expose only whether a key exists. Empty keys retain the active
credential; caller code never needs to load or return that credential itself.
"""
from __future__ import annotations

from pydantic import BaseModel

class TextModelSettings(BaseModel):
    base_url: str
    model: str
    api_key: str = ''


def text_settings():
    from bookmind.services.config import get_model_catalog_service
    catalog_service = get_model_catalog_service()
    catalog = catalog_service.load()
    profile = catalog_service.get_active_profile(catalog, 'llm') or {}
    model = catalog_service.get_active_model(catalog, 'llm') or {}
    return {'base_url': profile.get('base_url', 'https://api.deepseek.com'),
            'model': model.get('model', ''), 'has_api_key': bool(profile.get('api_key'))}


def save_text_settings(body):
    from bookmind.services.config import get_model_catalog_service
    from bookmind.services.llm.config import clear_llm_config_cache
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
