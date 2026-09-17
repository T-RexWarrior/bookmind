"""ModelRouter — ARCHITECTURE.md §8.

A single interface that hides model differences. The product path speaks the
official DeepSeek OpenAI-compatible API and degrades conservatively when the
service is unavailable.

Responsibilities:
  - ``complete(task, messages, output_schema=None)`` — chat completion with a
    primary model + fallbacks, timeout, retries, and JSON-mode extraction.
  - ``embed(texts)`` — embeddings with a deterministic offline fallback so the
    retrieval layer is testable without the gateway.
  - ``expand_query(query)`` — translate/expand cross-language textbook terms.
  - ``rerank(query, passages)`` — optional; only used when a live reranker is
    available and shown to help (LEARNING_MODEL/ARCHITECTURE: reranker is an
    enhancement, not a core dependency).
  - ``healthcheck()`` — verify the configured models are callable before use.
  - every call records model, latency, tokens, success, prompt_version.

The router is the *only* place that talks to a network model. Agents and the
engine never import the gateway directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .schemas import ModelResult, EmbeddingResult, RerankResult


# --- configuration -------------------------------------------------------

@dataclass
class ModelConfig:
    """A single model endpoint with task routing metadata."""

    model: str
    role: str  # "chat" | "embedding" | "rerank" | "chat_rerank"
    base_url: str = "https://api.deepseek.com"
    timeout: float = 60.0
    retries: int = 1
    temperature: float = 0.2
    # Reasoning models may spend a substantial portion of this budget before
    # emitting final ``content``.  4096 prevents false "model unavailable"
    # fallbacks caused by an empty final answer at very small budgets.
    max_tokens: int = 4096
    supports_json_mode: bool = True
    # DeepSeek enables high-effort thinking by default. Deterministic RAG
    # stages can disable it through the provider's official Chat Completions
    # parameter, avoiding reasoning-only truncation before JSON is emitted.
    thinking_mode: str | None = None


@dataclass
class RouterConfig:
    """Per-task primary + fallback chains (ARCHITECTURE §8)."""

    chat_primary: ModelConfig = field(
        default_factory=lambda: ModelConfig("deepseek-flash", "chat")
    )
    # One official provider is intentionally used.  Transport retries and the
    # circuit breaker provide stability without silently switching vendors.
    chat_fallbacks: tuple[ModelConfig, ...] = ()
    embedding: ModelConfig = field(
        default_factory=lambda: ModelConfig("qwen3-embedding", "embedding", timeout=90.0)
    )
    reranker: ModelConfig = field(
        default_factory=lambda: ModelConfig(
            "deepseek-flash", "chat_rerank", timeout=20.0, max_tokens=1024,
        )
    )
    prompt_version: str = "prompt_v1"
    api_key_env: str = "DEEPSEEK_API_KEY"
    api_key_file: str = ""
    # DeepSeek's official API currently has no embeddings endpoint.  Product
    # configuration disables dense retrieval instead of mixing incompatible
    # local and remote vector spaces.  Generic/offline tests may leave it on.
    embedding_enabled: bool = True
    rerank_via_chat: bool = False
    # When True, live network calls are attempted. When False, only offline
    # fixtures are used — the deterministic demo/CI path.
    live: bool = True
    total_chat_timeout: float = 25.0
    breaker_failures: int = 3
    breaker_window: float = 300.0
    breaker_cooldown: float = 120.0


def _api_key(cfg: RouterConfig) -> str | None:
    # Prefer an explicit env var.  A file pointer keeps a local credential out
    # of the repository and Settings/log serialization.
    value = (os.environ.get(cfg.api_key_env) or "").strip()
    if value:
        return value
    if not cfg.api_key_file:
        return None
    try:
        raw = Path(cfg.api_key_file).expanduser().read_text("utf-8").strip()
    except OSError:
        return None
    line = next((item.strip() for item in raw.splitlines()
                 if item.strip() and not item.lstrip().startswith("#")), "")
    if "=" in line:
        _, _, line = line.partition("=")
    return line.strip().strip("'\"") or None


def _transport_error_message(error: Exception) -> str:
    """Turn low-level connection failures into an actionable user message.

    Windows error 10013 is especially easy to misread: the local BookMind API
    can be healthy while the process is blocked from opening an outbound
    socket to the model gateway. Keeping this distinction in the result makes
    the UI and the packaged application's console useful to judges.
    """
    reason = getattr(error, "reason", None)
    details = " ".join(str(value) for value in (error, reason) if value)
    winerror = getattr(error, "winerror", None) or getattr(reason, "winerror", None)
    if winerror == 10013 or "10013" in details or "PermissionError" in details:
        return (
            "模型网关连接被本机网络权限拦截（Windows 错误 10013）。"
            "请允许 BookMind.exe 访问外网 HTTPS/443，或检查防火墙、代理和安全软件设置。"
        )
    if isinstance(error, TimeoutError) or "timed out" in details.lower():
        return "模型网关连接超时，请检查网络、代理或网关状态后重试。"
    return f"模型网关连接失败：{details or type(error).__name__}"


# --- the router ----------------------------------------------------------

class ModelRouter:
    """The single LLM access point. Network calls live here and nowhere else."""

    def __init__(self, cfg: RouterConfig | None = None, *, http: Callable[..., Any] | None = None) -> None:
        self.cfg = cfg or RouterConfig()
        # ``http`` is injectable for testing; production uses _default_http.
        self._http = http or _default_http
        self._call_log: list[dict[str, Any]] = []
        self._breaker: dict[str, tuple[int, float, float]] = {}
        self._breaker_lock = threading.Lock()
        self._trace_capture: ContextVar[dict[str, Any] | None] = ContextVar(
            "bookmind_llm_trace", default=None,
        )

    @contextmanager
    def trace_capture(self, run_id: str, *, capture_content: bool = False):
        """Associate router telemetry with one run without exposing it by default."""
        session: dict[str, Any] = {"run_id": run_id, "capture_content": capture_content, "calls": []}
        token = self._trace_capture.set(session)
        try:
            yield session["calls"]
        finally:
            self._trace_capture.reset(token)

    def _record_call(
        self, task: str, model: str, latency: float, ok: bool, usage: dict,
        error: str, *, messages: list[dict[str, str]] | None = None, response: str | None = None,
    ) -> None:
        entry = self._log_entry(task, model, latency, ok, usage, error)
        self._call_log.append(entry)
        session = self._trace_capture.get()
        if session is None:
            return
        trace_entry = {**entry, "latency_ms": round(latency * 1000)}
        # Raw error details can leak provider internals; expose a status only.
        trace_entry.pop("error", None)
        trace_entry["error_code"] = "OK" if ok else "CALL_FAILED"
        if session["capture_content"]:
            trace_entry["messages"] = messages or []
            trace_entry["response"] = response or ""
        session["calls"].append(trace_entry)

    # --- chat ---------------------------------------------------------------

    def complete(
        self,
        task: str,
        messages: list[dict[str, str]],
        *,
        output_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResult:
        """Run a chat completion with primary → fallback chain.

        Returns a :class:`ModelResult`. On total failure, ``ok=False`` with a
        ``fallback`` marker; callers (Tutor/Diagnostician) must degrade
        gracefully (template answers, NEEDS_REVIEW) rather than crash.
        """
        # Environment overrides can name the same model as a default fallback.
        # Do not call an already-failed endpoint twice in one user turn.
        chain: list[ModelConfig] = []
        seen_models: set[str] = set()
        for cfg in (self.cfg.chat_primary, *self.cfg.chat_fallbacks):
            if cfg.model in seen_models:
                continue
            seen_models.add(cfg.model)
            chain.append(cfg)
        last_error = ""
        started = time.monotonic()
        for cfg in chain:
            if time.monotonic() - started >= self.cfg.total_chat_timeout:
                last_error = "问答总时间预算已用完"
                break
            if self._circuit_open(cfg.model):
                last_error = f"{cfg.model} 暂时熔断"
                continue
            res = self._complete_one(
                cfg, task, messages, output_schema, temperature, max_tokens,
            )
            if res.ok:
                self._record_model_success(cfg.model)
                return res
            self._record_model_failure(cfg.model)
            last_error = res.error or ""
        # All models failed — return a structured degradation, never raise.
        logging.getLogger("bookmind.llm").warning(
            "chat completion degraded task=%s models=%s error=%s",
            task,
            ",".join(cfg.model for cfg in chain),
            last_error or "all models failed",
        )
        return ModelResult(
            ok=False, task=task, model="all-failed", content=None,
            raw=None, prompt_version=self.cfg.prompt_version,
            error=last_error or "all models failed", fallback=True,
        )

    def _circuit_open(self, model: str) -> bool:
        now = time.monotonic()
        with self._breaker_lock:
            count, last_failure, open_until = self._breaker.get(model, (0, 0.0, 0.0))
            if open_until > now:
                return True
            if open_until and open_until <= now:
                self._breaker[model] = (0, 0.0, 0.0)
            return False

    def _record_model_success(self, model: str) -> None:
        with self._breaker_lock:
            self._breaker[model] = (0, 0.0, 0.0)

    def _record_model_failure(self, model: str) -> None:
        now = time.monotonic()
        with self._breaker_lock:
            count, last_failure, _ = self._breaker.get(model, (0, 0.0, 0.0))
            count = count + 1 if now - last_failure <= self.cfg.breaker_window else 1
            open_until = now + self.cfg.breaker_cooldown if count >= self.cfg.breaker_failures else 0.0
            self._breaker[model] = (count, now, open_until)

    def _complete_one(
        self,
        cfg: ModelConfig,
        task: str,
        messages: list[dict[str, str]],
        output_schema: dict[str, Any] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> ModelResult:
        key = _api_key(self.cfg)
        if not self.cfg.live or not key:
            return ModelResult(
                ok=False, task=task, model=cfg.model, content=None, raw=None,
                prompt_version=self.cfg.prompt_version,
                error="offline mode (no live flag or no api key)", fallback=True,
            )

        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature if temperature is None else temperature,
            "max_tokens": cfg.max_tokens if max_tokens is None else max_tokens,
        }
        if cfg.thinking_mode in {"enabled", "disabled"}:
            payload["thinking"] = {"type": cfg.thinking_mode}
        # JSON mode helps structured-output tasks (Diagnostician, Book Mapper).
        expects_json = output_schema is not None
        use_json_mode = expects_json and cfg.supports_json_mode
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        attempts = cfg.retries + 1
        for attempt in range(attempts):
            t0 = time.perf_counter()
            try:
                status, body = self._http(
                    cfg.base_url + "/chat/completions", payload, key, cfg.timeout,
                )
            except Exception as e:
                # Network/timeout/connection errors → treat as a failed attempt
                # and fall through to the next model in the chain (ARCHITECTURE
                # §8: "所有模型均失败时返回可理解的降级状态，不让程序崩溃").
                latency = time.perf_counter() - t0
                self._record_call(task, cfg.model, latency, False, {}, f"transport: {e}", messages=messages)
                last_error = _transport_error_message(e)
                continue
            latency = time.perf_counter() - t0
            if status == 200:
                try:
                    parsed = json.loads(body)
                    msg = parsed["choices"][0]["message"]
                    content = msg.get("content")
                    # Some reasoning models return content=None with the answer
                    # in reasoning_content; treat that as a fallback signal.
                    usage = parsed.get("usage", {})
                    if content:
                        self._record_call(task, cfg.model, latency, True, usage, "", messages=messages, response=content)
                        # Some gateways/models do not support response_format
                        # reliably, but still follow the JSON-only prompt. Parse
                        # JSON whenever the caller supplied a schema, regardless
                        # of whether transport-level JSON mode was enabled.
                        extracted = _maybe_extract_json(content, expects_json)
                        return ModelResult(
                            ok=True, task=task, model=cfg.model, content=content,
                            raw=parsed, parsed_json=extracted,
                            prompt_version=self.cfg.prompt_version,
                            latency=latency, tokens=usage,
                        )
                    self._record_call(task, cfg.model, latency, False, usage,
                                      "empty content (reasoning-only model)", messages=messages)
                    return ModelResult(
                        ok=False, task=task, model=cfg.model, content=None,
                        raw=parsed, prompt_version=self.cfg.prompt_version,
                        error="empty content (reasoning-only model)", fallback=True,
                    )
                except (json.JSONDecodeError, KeyError, IndexError) as e:
                    self._record_call(task, cfg.model, latency, False, {}, f"parse: {e}", messages=messages)
                    last_error = f"parse: {e}"
                    continue
            else:
                self._record_call(task, cfg.model, latency, False, {}, f"http {status}", messages=messages)
                last_error = f"http {status}: {body[:200]}"
                # 4xx (except 429) is unlikely to succeed on retry.
                if 400 <= status < 500 and status != 429:
                    break
        return ModelResult(
            ok=False, task=task, model=cfg.model, content=None, raw=None,
            prompt_version=self.cfg.prompt_version,
            error=last_error or f"{cfg.model} failed", fallback=True,
        )

    # --- embeddings ---------------------------------------------------------

    def embed_local(self, texts: list[str]) -> EmbeddingResult:
        """Create fast deterministic vectors without a network request.

        Used while restoring a large persisted textbook index: rebuilding the
        lexical index must not make the learner wait for thousands of remote
        embeddings before their first question can be answered.
        """
        return EmbeddingResult(
            ok=True,
            model="offline-hash-256",
            vectors=[_hash_embedding(text, 256) for text in texts],
            dim=256,
            fallback=True,
        )

    def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a batch. Falls back to a deterministic hashing vector so the
        retrieval layer is fully testable offline.

        The offline vector is a 256-dim hashed bag-of-tokens vector with L2
        normalisation — not semantically meaningful, but deterministic, fast,
        and good enough to exercise the dense/RRF/VectorStore contracts and
        the offline demo corpus.  The DeepSeek product path disables this
        capability because the official API has no embeddings endpoint.
        """
        if not self.cfg.embedding_enabled:
            return EmbeddingResult(
                ok=False, model="disabled", vectors=[], dim=0, fallback=True,
            )
        key = _api_key(self.cfg)
        if not self.cfg.live or not key:
            return self.embed_local(texts)
        cfg = self.cfg.embedding
        t0 = time.perf_counter()
        try:
            status, body = self._http(cfg.base_url + "/embeddings", {
                "model": cfg.model, "input": texts,
            }, key, cfg.timeout)
        except Exception as e:
            # Timeout/network error → degrade to offline vectors, never crash.
            self._call_log.append(self._log_entry("embed", cfg.model, time.perf_counter() - t0, False, {}, f"transport: {e}"))
            return self.embed_local(texts)
        latency = time.perf_counter() - t0
        if status == 200:
            try:
                parsed = json.loads(body)
                vectors = [d["embedding"] for d in parsed["data"]]
                dim = len(vectors[0]) if vectors else 0
                self._call_log.append(self._log_entry("embed", cfg.model, latency, True, parsed.get("usage", {}), ""))
                return EmbeddingResult(ok=True, model=cfg.model, vectors=vectors, dim=dim, fallback=False)
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                self._call_log.append(self._log_entry("embed", cfg.model, latency, False, {}, f"parse: {e}"))
        else:
            self._call_log.append(self._log_entry("embed", cfg.model, latency, False, {}, f"http {status}"))
        # Degrade to offline vectors rather than failing the whole pipeline.
        return self.embed_local(texts)

    # --- retrieval query expansion -----------------------------------------

    def expand_query(self, query: str) -> list[str]:
        """Return a few literal textbook search phrases for ``query``.

        DeepSeek's official API has no embedding endpoint.  This small JSON
        call closes the most important gap left by lexical-only retrieval:
        Chinese learners can still find concepts in an English textbook.  A
        failure is deliberately a no-op so retrieval remains available.
        """
        key = _api_key(self.cfg)
        if not query.strip() or not self.cfg.live or not key:
            return []
        # Query planning is user-facing recall work, so use the primary chat
        # budget rather than the deliberately tiny shortlist-rerank budget.
        cfg = self.cfg.chat_primary
        messages = [
            {
                "role": "system",
                "content": (
                    "你是计算机教材检索词生成器。把用户问题改写成适合在教材原文中"
                    "逐词搜索的短语；若问题是中文，同时给出准确的英文术语、常见缩写和"
                    "关键符号。可以加入教材最可能使用的子概念、组成项或操作名称作为"
                    "候选检索词，但不要写解释或完整答案。若问题含有多个并列子问，"
                    "必须为每个子问分别生成至少一个可独立搜索的短语。只返回 JSON："
                    "{\"queries\":[\"短语1\",\"短语2\"]}。最多 6 个短语，"
                    "每个短语不超过 12 个词。至少一个短语应包含用于直接定位答案正文的"
                    "具体术语，而不只是重复问题中的泛词；第一个短语应尽量合并主题名、"
                    "关键组成项、典型操作或复杂度符号，形成高区分度的覆盖检索词。"
                ),
            },
            {"role": "user", "content": f"用户问题：{query}"},
        ]
        result = self._complete_one(
            cfg,
            "retrieval_query_expansion",
            messages,
            {"type": "object", "required": ["queries"]},
            0.0,
            # deepseek-flash may spend part of the completion budget on
            # reasoning before emitting the tiny JSON object.
            min(1024, cfg.max_tokens),
        )
        data = result.parsed_json if result.ok else None
        raw_queries = data.get("queries") if isinstance(data, dict) else None
        if not isinstance(raw_queries, list):
            return []
        expanded: list[str] = []
        seen = {query.strip().casefold()}
        for value in raw_queries:
            if not isinstance(value, str):
                continue
            phrase = " ".join(value.split()).strip()
            marker = phrase.casefold()
            if not phrase or marker in seen:
                continue
            seen.add(marker)
            expanded.append(phrase[:160])
            if len(expanded) == 6:
                break
        return expanded

    # --- rerank (optional) --------------------------------------------------

    def rerank(self, query: str, passages: list[str]) -> RerankResult:
        """Score passages against a query. Reranker is an enhancement; if it is
        unavailable we return a no-op (caller keeps RRF order)."""
        key = _api_key(self.cfg)
        if not self.cfg.live or not key:
            return RerankResult(ok=False, model="none", scores=[], fallback=True)
        cfg = self.cfg.reranker
        if self.cfg.rerank_via_chat or cfg.role == "chat_rerank":
            # DeepSeek does not expose a dedicated rerank endpoint.  Use its
            # official JSON-capable chat endpoint over the small RRF shortlist,
            # never over the whole book.  Indexes, not model-supplied ids, keep
            # the response deterministic and easy to validate.
            numbered = "\n\n".join(
                f"[{index}] {passage[:1400]}"
                for index, passage in enumerate(passages)
            )
            messages = [
                {
                    "role": "system",
                    "content": (
                    "你是教材检索重排器。判断每个候选片段对问题的直接支持程度，"
                    "问题可能包含多个子问；能直接支持其中任何一个子问的定义、公式、"
                    "算法步骤或复杂度结论都应得到高分，不能因只支持部分子问而判为无关。"
                    "只返回 JSON：{\"scores\":[0到1之间的数字]}。"
                        "scores 数量和候选数量必须完全一致，不要解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"问题：{query}\n\n候选片段：\n{numbered}",
                },
            ]
            result = self._complete_one(
                cfg, "rerank", messages,
                {"type": "object", "required": ["scores"]},
                0.0, min(4096, cfg.max_tokens),
            )
            data = result.parsed_json if result.ok else None
            raw_scores = data.get("scores") if isinstance(data, dict) else None
            if isinstance(raw_scores, list) and len(raw_scores) == len(passages):
                try:
                    scores = [max(0.0, min(1.0, float(value))) for value in raw_scores]
                except (TypeError, ValueError):
                    scores = []
                if len(scores) == len(passages):
                    return RerankResult(
                        ok=True, model=cfg.model, scores=scores, fallback=False,
                    )
            return RerankResult(
                ok=False, model=cfg.model, scores=[], fallback=True,
            )
        t0 = time.perf_counter()
        try:
            status, body = self._http(cfg.base_url + "/rerank", {
                "model": cfg.model, "query": query, "documents": passages,
            }, key, cfg.timeout)
        except Exception as e:
            # Timeout/network error → no-op rerank, never crash.
            self._call_log.append(self._log_entry("rerank", cfg.model, time.perf_counter() - t0, False, {}, f"transport: {e}"))
            return RerankResult(ok=False, model="none", scores=[], fallback=True)
        latency = time.perf_counter() - t0
        if status == 200:
            try:
                parsed = json.loads(body)
                results = sorted(parsed["results"], key=lambda r: r["index"])
                scores = [r["relevance_score"] for r in results]
                self._call_log.append(self._log_entry("rerank", cfg.model, latency, True, {}, ""))
                return RerankResult(ok=True, model=cfg.model, scores=scores, fallback=False)
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                self._call_log.append(self._log_entry("rerank", cfg.model, latency, False, {}, f"parse: {e}"))
        else:
            self._call_log.append(self._log_entry("rerank", cfg.model, latency, False, {}, f"http {status}"))
        return RerankResult(ok=False, model=cfg.model, scores=[], fallback=True)

    # --- healthcheck --------------------------------------------------------

    def healthcheck(self) -> dict[str, Any]:
        """Verify the configured models are callable (ARCHITECTURE §8 startup
        check). Returns a per-capability status dict; never raises."""
        out: dict[str, Any] = {
            "chat": False,
            "embedding": None if not self.cfg.embedding_enabled else False,
            "reranker": False,
            "chat_model": self.cfg.chat_primary.model,
            "embedding_model": self.cfg.embedding.model,
            "reranker_model": self.cfg.reranker.model,
        }
        try:
            # Eight tokens is too small for models that internally reason before
            # emitting content and used to create false-negative health checks.
            cr = self.complete(
                "healthcheck",
                [{"role": "user", "content": "Reply with exactly: pong"}],
                max_tokens=4096,
                temperature=0.0,
            )
            out["chat"] = cr.ok and not cr.fallback
            out["chat_model"] = cr.model
        except Exception:  # pragma: no cover - defensive
            out["chat"] = False
        if self.cfg.embedding_enabled:
            try:
                er = self.embed(["ping"])
                # The deterministic vector keeps tests usable, but it must not
                # be reported as a healthy remote model.
                out["embedding"] = er.ok and not er.fallback
                out["embedding_dim"] = er.dim
                out["embedding_model"] = er.model
                out["embedding_fallback"] = er.fallback
            except Exception:  # pragma: no cover
                out["embedding"] = False
        else:
            out["embedding_model"] = "disabled"
            out["embedding_fallback"] = False
        try:
            rr = self.rerank("ping", ["a"])
            out["reranker"] = rr.ok and not rr.fallback
            out["reranker_model"] = rr.model
            out["reranker_fallback"] = rr.fallback
        except Exception:  # pragma: no cover
            out["reranker"] = False
        out["live"] = self.cfg.live and bool(_api_key(self.cfg))
        embedding_ok = out["embedding"] is True or not self.cfg.embedding_enabled
        out["healthy"] = bool(out["chat"] and embedding_ok and out["reranker"])
        return out

    # --- observability ------------------------------------------------------

    def call_log(self) -> list[dict[str, Any]]:
        """A copy of every call's recorded telemetry (model, latency, tokens)."""
        return list(self._call_log)

    @property
    def live_available(self) -> bool:
        """Whether this router can make an authenticated provider call."""
        return bool(self.cfg.live and _api_key(self.cfg))

    def _log_entry(self, task: str, model: str, latency: float, ok: bool, usage: dict, error: str) -> dict[str, Any]:
        return {
            "task": task, "model": model, "latency": round(latency, 4),
            "ok": ok, "tokens": usage, "error": error,
            "prompt_version": self.cfg.prompt_version,
        }


# --- HTTP transport (injectable for tests) -------------------------------

def _default_http(url: str, payload: dict, key: str, timeout: float) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:  # pragma: no cover - network errors
        return -1, repr(e)


# --- JSON extraction ------------------------------------------------------

def _maybe_extract_json(content: str, want_json: bool) -> Any | None:
    """Extract a JSON object from a model response.

    Models usually obey ``response_format=json_object`` but sometimes wrap the
    JSON in prose or code fences. We try direct parse, then a fenced/brace scan.
    """
    if not want_json:
        return None
    text = content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip code fences.
    if text.startswith("```"):
        inner = text.split("```")
        for chunk in inner:
            chunk = chunk.strip()
            if chunk.startswith("{") or chunk.startswith("["):
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    continue
    # Find the first balanced {...} block.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# --- deterministic offline embedding fallback -----------------------------

def _hash_embedding(text: str, dim: int = 256) -> list[float]:
    """A deterministic, L2-normalised hashed bag-of-tokens vector.

    This is *not* semantically meaningful — it exists so the dense retrieval
    and VectorStore contracts are exercised in CI without the gateway. The
    live path replaces it with qwen3-embedding. Tokenisation is a simple
    Unicode-aware lowercase split so Chinese and English both work.
    """
    vec = [0.0] * dim
    for tok in _tokenize(text):
        h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
        # A second, signed bucket so collisions don't all align positive.
        h2 = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        vec[h2 % dim] += 0.5
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _tokenize(text: str) -> list[str]:
    """Minimal tokeniser shared with the BM25 index for consistency.

    Latin/digit runs stay intact. Chinese runs emit overlapping bigrams and
    trigrams (plus a short whole phrase), which preserves terms such as
    "改进版" and "不支持" without letting ubiquitous single
    characters dominate BM25 scores.
    """
    toks: list[str] = []
    latin = ""
    cjk = ""

    def flush_latin() -> None:
        nonlocal latin
        if latin:
            toks.append(latin)
            latin = ""

    def flush_cjk() -> None:
        nonlocal cjk
        if not cjk:
            return
        if len(cjk) == 1:
            toks.append(cjk)
        else:
            toks.extend(cjk[index:index + 2] for index in range(len(cjk) - 1))
            if len(cjk) >= 3:
                toks.extend(cjk[index:index + 3] for index in range(len(cjk) - 2))
            if len(cjk) <= 8:
                toks.append(cjk)
        cjk = ""

    for ch in text.lower():
        if "一" <= ch <= "鿿":
            flush_latin()
            cjk += ch
        elif ch.isalnum() or ch in {"_", "+", "#"}:
            flush_cjk()
            latin += ch
        else:
            flush_latin()
            flush_cjk()
    flush_latin()
    flush_cjk()
    return toks
