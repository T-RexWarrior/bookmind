"""Safely configure the local campus API key without printing it.

The generated ``.env`` is gitignored. Existing non-secret settings are kept,
and an existing API-key line is replaced rather than duplicated.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
from pathlib import Path


KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]+")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Text file containing the campus API key")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Target .env path")
    args = parser.parse_args()

    match = KEY_PATTERN.search(args.source.read_text("utf-8"))
    if match is None:
        raise SystemExit("No campus API key was found in the source file.")
    key = match.group(0)

    env_path = args.env.resolve()
    template = Path(".env.example")
    if env_path.exists():
        lines = env_path.read_text("utf-8").splitlines()
    elif template.is_file():
        lines = template.read_text("utf-8").splitlines()
    else:
        lines = []

    output: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith("USTC_LLM_API_KEY=") or line.strip().startswith("# USTC_LLM_API_KEY="):
            if not replaced:
                output.append(f"USTC_LLM_API_KEY={key}")
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.extend(["", f"USTC_LLM_API_KEY={key}"])

    temporary = env_path.with_name(f".{env_path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, env_path)
    print(f"Campus API configured in {env_path.name}; the key was not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
