"""Tests for the .env auto-loader in bookmind.config.

The loader injects a developer's ``.env`` into ``os.environ`` so the LLM API
key (read by env-var name, not a Settings field) can be set by editing the file
instead of exporting per shell. Tests must be hermetic: they write a temp .env,
call the loader directly, and restore the environment afterwards.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bookmind.config import _load_dotenv

BACKEND_DIR = str(Path(__file__).resolve().parents[1] / "backend")


def _run_in_clean_subprocess(code: str) -> str:
    """Run code in a fresh subprocess so the module-level _load_dotenv() call
    and the lru_cached get_settings() don't leak between tests.

    ``BOOKMIND_NO_DOTENV`` is set so the module-level ``_load_dotenv(".env")``
    at import time does NOT load a developer's real .env from the cwd (which
    would otherwise seed the real API key via setdefault and mask the value the
    test is asserting on). Each test calls ``_load_dotenv`` explicitly on its
    own temp file.
    """
    import subprocess
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("BOOKMIND_TEST_KEY", None)
    env["BOOKMIND_NO_DOTENV"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_load_dotenv_injects_unprefixed_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-from-env-file\n", encoding="utf-8")
    code = (
        f"import os, sys; sys.path.insert(0, {BACKEND_DIR!r}); "
        "from bookmind.config import _load_dotenv; "
        f"_load_dotenv({str(env_file)!r}); "
        "print(os.environ.get('DEEPSEEK_API_KEY'))"
    )
    assert _run_in_clean_subprocess(code) == "sk-from-env-file"


def test_load_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    """A variable already in the real environment wins over the file."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-real-env")
    monkeypatch.delenv("BOOKMIND_NO_DOTENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-from-file\n", encoding="utf-8")
    _load_dotenv(str(env_file))
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-real-env"


def test_load_dotenv_strips_quotes_and_comments(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOKMIND_NO_DOTENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment line\nKEY_QUOTED="sk-quoted-value"\n'
        "KEY_BARE=sk-bare # trailing comment\n\n",
        encoding="utf-8",
    )
    _load_dotenv(str(env_file))
    assert os.environ["KEY_QUOTED"] == "sk-quoted-value"
    assert os.environ["KEY_BARE"] == "sk-bare"


def test_load_dotenv_skipped_when_no_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOKMIND_NO_DOTENV", raising=False)
    # No file at the path → no error, no injection.
    _load_dotenv(str(tmp_path / "missing.env"))


def test_load_dotenv_disabled_by_flag(tmp_path):
    """The test suite sets BOOKMIND_NO_DOTENV so the module-level auto-load at
    import time skips a developer's real .env (with a real key) — it never
    turns the offline test suite live. The auto-load only runs at import, so we
    verify it in a subprocess: with the flag set, importing config must NOT pick
    up the cwd .env's key; without it, it must."""
    env_file = tmp_path / ".env"
    env_file.write_text("BOOKMIND_DISABLED_KEY=should-not-auto-load\n", encoding="utf-8")
    # With the flag set → auto-load skipped → key absent.
    code_off = (
        f"import os, sys; sys.path.insert(0, {BACKEND_DIR!r}); "
        f"os.chdir({str(tmp_path)!r}); "
        "import bookmind.config; "
        "print(os.environ.get('BOOKMIND_DISABLED_KEY') or '')"
    )
    assert _run_in_clean_subprocess(code_off) == ""
    # Without the flag → auto-load runs → key present.
    import subprocess
    env = dict(os.environ)
    env.pop("BOOKMIND_NO_DOTENV", None)
    env.pop("BOOKMIND_DISABLED_KEY", None)
    code_on = (
        f"import os, sys; sys.path.insert(0, {BACKEND_DIR!r}); "
        f"os.chdir({str(tmp_path)!r}); "
        "import bookmind.config; "
        "print(os.environ.get('BOOKMIND_DISABLED_KEY') or '')"
    )
    proc = subprocess.run([sys.executable, "-c", code_on], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "should-not-auto-load"


def test_settings_sees_dotenv_key_via_llm_api_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-settings-sees-it\n", encoding="utf-8")
    code = (
        f"import os, sys; sys.path.insert(0, {BACKEND_DIR!r}); "
        "from bookmind.config import get_settings, _load_dotenv; "
        f"_load_dotenv({str(env_file)!r}); "
        "print(get_settings().llm_api_key())"
    )
    assert _run_in_clean_subprocess(code) == "sk-settings-sees-it"
