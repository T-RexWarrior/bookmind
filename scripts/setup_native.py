"""Initialize the copied runtime and import only local API settings from v2.

No credentials are printed or written outside the gitignored data directory.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deeptutor.services.config import ensure_runtime_settings_files
from deeptutor.services.file_io import atomic_write_json
from deeptutor.services.path_service import get_path_service

ensure_runtime_settings_files()
settings = get_path_service().get_settings_dir()
system_file = settings / 'system.json'
system = json.loads(system_file.read_text(encoding='utf-8'))
system.update(backend_port=8019, frontend_port=8018, version_check_enabled=False,
              sandbox_allow_subprocess=False)
atomic_write_json(system_file, system)
interface_file = settings / 'interface.json'
interface = json.loads(interface_file.read_text(encoding='utf-8')) if interface_file.exists() else {}
interface.update(language='zh', response_language='zh', enabled_tools=[], theme='snow')
atomic_write_json(interface_file, interface)

legacy_settings = ROOT.parent / 'BookMind-v2' / 'data' / 'settings.json'
if legacy_settings.exists():
    from deeptutor.bookmind.router import TextModelSettings, save_text_settings
    old = json.loads(legacy_settings.read_text(encoding='utf-8'))
    result = save_text_settings(TextModelSettings(base_url=old['base_url'], model=old['model'], api_key=old['api_key']))
    print(json.dumps({'native_model_configured': result['has_api_key'], 'model': result['model']}))
else:
    print('Use API settings in BookMind to configure a text model.')
