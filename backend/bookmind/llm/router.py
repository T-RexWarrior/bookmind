"""ModelRouter — ARCHITECTURE.md §8.

A single interface that hides model differences. It speaks the USTC campus
gateway (OpenAI-compatible) on the real path and degrades to deterministic
offline fixtures on the demo path, so the system never crashes when a model
is unavailable (PRODUCT_SPEC §8 "模型不可用时存在保守 fallback").

Responsibilities:
  - ``complete(task, messages, output_schema=None)`` — chat completion with a
    primary model + fallbacks, timeout, retries, and JSON-mode extraction.
  - ``embed(texts)`` — embeddings with a deterministic offline fallback so the
    retrieval layer is testable without the gateway.
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
from dataclasses import dataclass, field
from typing import Any, Callable

from .schemas import ModelResult, EmbeddingResult, RerankResult


# --- configuration -------------------------------------------------------

@dataclass
class ModelConfig:
    """A single model endpoint with task routing metadata."""

    model: str
    role: str  # "chat" | "embedding" | "rerank"
    base_url: str = "https://api.llm.ustc.edu.cn/v1"
    timeout: float = 60.0
    retries: int = 1
    temperature: float = 0.2
    # Reasoning models may spend a substantial portion of this budget before
    # emitting final ``content``.  4096 prevents false "model unavailable"
    # fallbacks caused by an empty final answer at very small budgets.
    max_tokens: int = 4096
    supports_json_mode: bool = True


@dataclass
class RouterConfig:
    """Per-task primary + fallback chains (ARCHITECTURE §8)."""

    chat_primary: ModelConfig = field(
        default_factory=lambda: ModelConfig("glm-5.2-107", "chat")
    )
    # Generic library defaults retain a fallback chain for injected/offline
    # callers. The product dependency config deliberately overrides this with
    # an empty tuple so every user-facing chat call stays on glm-5.2-107.
    chat_fallbacks: tuple[ModelConfig, ...] = (
        ModelConfig("deepseek-v4-flash", "chat"),
        ModelConfig("glm-5.3-flash", "chat", supports_json_mode=False),
    )
    embedding: ModelConfig = field(
        default_factory=lambda: ModelConfig("qwen3-embedding", "embedding", timeout=90.0)
    )
    reranker: ModelConfig = field(
        default_factory=lambda: ModelConfig("qwen3-reranker", "rerank", timeout=60.0)
    )
    prompt_version: str = "prompt_v1"
    api_key_env: str = "USTC_LLM_API_KEY"
    # When True, live network calls are attempted. When False, only offline
    # fixtures are used — the deterministic demo/CI path.
    live: bool = True


def _api_key(cfg: RouterConfig) -> str | None:
    # Prefer an explicit env var; the demo fixture path also works with no key.
    return os.environ.get(cfg.api_key_env)


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
        for cfg in chain:
            res = self._complete_one(
                cfg, task, messages, output_schema, temperature, max_tokens,
            )
            if res.ok:
                return res
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
                self._call_log.append(self._log_entry(task, cfg.model, latency, False, {}, f"transport: {e}"))
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
                        self._call_log.append(self._log_entry(task, cfg.model, latency, True, usage, ""))
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
                    self._call_log.append(self._log_entry(
                        task, cfg.model, latency, False, usage,
                        "empty content (reasoning-only model)",
                    ))
                    return ModelResult(
                        ok=False, task=task, model=cfg.model, content=None,
                        raw=parsed, prompt_version=self.cfg.prompt_version,
                        error="empty content (reasoning-only model)", fallback=True,
                    )
                except (json.JSONDecodeError, KeyError, IndexError) as e:
                    self._call_log.append(self._log_entry(task, cfg.model, latency, False, {}, f"parse: {e}"))
                    last_error = f"parse: {e}"
                    continue
            else:
                self._call_log.append(self._log_entry(task, cfg.model, latency, False, {}, f"http {status}"))
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
        the offline demo corpus. The live path uses qwen3-embedding (4096-dim).
        """
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

    # --- rerank (optional) --------------------------------------------------

    def rerank(self, query: str, passages: list[str]) -> RerankResult:
        """Score passages against a query. Reranker is an enhancement; if it is
        unavailable we return a no-op (caller keeps RRF order)."""
        key = _api_key(self.cfg)
        if not self.cfg.live or not key:
            return RerankResult(ok=False, model="none", scores=[], fallback=True)
        cfg = self.cfg.reranker
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
            "embedding": False,
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
        try:
            er = self.embed(["ping"])
            # The deterministic 256-dimensional vector keeps the application
            # usable, but it must not be reported as a healthy remote model.
            out["embedding"] = er.ok and not er.fallback
            out["embedding_dim"] = er.dim
            out["embedding_model"] = er.model
            out["embedding_fallback"] = er.fallback
        except Exception:  # pragma: no cover
            out["embedding"] = False
        try:
            rr = self.rerank("ping", ["a"])
            out["reranker"] = rr.ok and not rr.fallback
            out["reranker_model"] = rr.model
            out["reranker_fallback"] = rr.fallback
        except Exception:  # pragma: no cover
            out["reranker"] = False
        out["live"] = self.cfg.live and bool(_api_key(self.cfg))
        out["healthy"] = bool(out["chat"] and out["embedding"] and out["reranker"])
        return out

    # --- observability ------------------------------------------------------

    def call_log(self) -> list[dict[str, Any]]:
        """A copy of every call's recorded telemetry (model, latency, tokens)."""
        return list(self._call_log)

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
