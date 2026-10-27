from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import httpx

from . import store


DEFAULTS = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key": "", "temperature": 0.4, "max_tokens": 6000, "timeout": 180}


def settings():
    path = store.DATA / "settings.json"
    return {**DEFAULTS, **(json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {})}


def save_settings(values):
    value = settings()
    value.update(values)
    path = store.DATA / "settings.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def safe_error(exc):
    if isinstance(exc, httpx.TimeoutException):
        return "等待模型回复超时，请稍后重试，或在高级设置中延长超时时间。"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接模型服务，请检查 API 地址和网络连接。"
    value = str(exc)
    key = settings().get("api_key", "")
    if key:
        value = value.replace(key, "[已隐藏]")
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", value)[:350]


async def complete(messages, *, partial: Callable | None = None, max_tokens=None, config=None):
    cfg = config or settings()
    if not cfg.get("api_key"):
        raise ValueError("请先在 API 设置中填写密钥并测试连接。")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": cfg["model"], "messages": messages, "temperature": cfg["temperature"], "max_tokens": max_tokens or cfg["max_tokens"], "stream": True}
    if "deepseek" in cfg["base_url"].lower():
        body["thinking"] = {"type": "disabled"}
    content = ""
    import time
    last_flush = 0.0
    finished = False
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg["timeout"], connect=20)) as client:
        async with client.stream("POST", url, headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                if response.status_code == 401:
                    raise ValueError("API 密钥无效或已过期（401），请检查设置。")
                raise ValueError(f"模型服务返回 HTTP {response.status_code}，请检查模型名称、额度与地址。")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    finished = True
                    break
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    raise ValueError("模型服务中断了生成，请稍后重试。")
                for choice in data.get("choices", []):
                    content += choice.get("delta", {}).get("content") or ""
                    if choice.get("finish_reason") == "length":
                        raise ValueError("模型输出达到长度上限，请在 API 设置增大输出长度，或缩小本次任务。")
                    if choice.get("finish_reason") == "stop":
                        finished = True
                if partial and time.monotonic() - last_flush > 0.4:
                    partial(content)
                    last_flush = time.monotonic()
    if not finished or not content.strip():
        raise ValueError("模型没有完整返回内容，请重试或检查 API 兼容性。")
    if partial:
        partial(content)
    return content


def parse_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(text[start:])[0]
            except json.JSONDecodeError:
                pass
    raise ValueError("模型返回的结构不完整，请重新生成。")
