"""L1 tests: ModelRouter — ARCHITECTURE.md §8.

Covers the offline fallback path (deterministic, no network) and the
live path via an injected fake HTTP transport. The router must never raise
on model failure — it returns a structured degradation (PRODUCT_SPEC §8).
"""

from __future__ import annotations

import json

from bookmind.llm.router import (
    ModelConfig,
    ModelRouter,
    RouterConfig,
    _hash_embedding,
    _maybe_extract_json,
    _tokenize,
)
from bookmind.llm.schemas import ModelResult


# --- offline path --------------------------------------------------------

def test_offline_complete_returns_fallback_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg)
    res = r.complete("t", [{"role": "user", "content": "hi"}])
    assert res.ok is False
    assert res.fallback is True
    assert "offline" in res.error


def test_offline_complete_when_live_disabled():
    cfg = RouterConfig(live=False)
    r = ModelRouter(cfg)
    res = r.complete("t", [{"role": "user", "content": "hi"}])
    assert res.ok is False
    assert res.fallback is True


def test_offline_embed_is_deterministic_and_normalised():
    cfg = RouterConfig(live=False)
    r = ModelRouter(cfg)
    a = r.embed(["引用与对象"])
    b = r.embed(["引用与对象"])
    assert a.ok is True
    assert a.fallback is True
    assert a.dim == 256
    assert a.vectors == b.vectors  # deterministic
    # L2 normalised.
    norm = sum(v * v for v in a.vectors[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_offline_embed_distinct_texts_differ():
    cfg = RouterConfig(live=False)
    r = ModelRouter(cfg)
    res = r.embed(["引用与对象", "List 与 Set 的区别"])
    assert res.vectors[0] != res.vectors[1]


def test_offline_rerank_is_noop_fallback():
    cfg = RouterConfig(live=False)
    r = ModelRouter(cfg)
    res = r.rerank("q", ["a", "b"])
    assert res.ok is False
    assert res.fallback is True
    assert res.scores == []


def test_api_key_can_be_read_from_file_without_entering_config(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    key_file = tmp_path / "deepseek.txt"
    key_file.write_text("DEEPSEEK_API_KEY=sk-from-file\n", encoding="utf-8")
    body = json.dumps({
        "choices": [{"message": {"content": "ok"}}], "usage": {},
    })
    http, state = _fake_http_factory({"/chat/completions": (200, body)})
    router = ModelRouter(
        RouterConfig(live=True, api_key_file=str(key_file)), http=http,
    )

    result = router.complete("t", [{"role": "user", "content": "x"}])

    assert result.ok is True
    assert state["calls"][0]["key"] == "sk-from-file"
    assert "sk-from-file" not in repr(router.cfg)


# --- live path via fake HTTP ---------------------------------------------

def _fake_http_factory(responses):
    """Build a fake transport that returns canned (status, body) per URL suffix.

    ``responses`` maps a url substring (e.g. '/chat/completions') to either a
    (status, body) tuple or a list of such tuples (consumed in order).
    """
    state = {"calls": []}

    def http(url, payload, key, timeout):
        state["calls"].append({"url": url, "payload": payload, "key": key})
        for suffix, resp in responses.items():
            if url.endswith(suffix):
                if isinstance(resp, list):
                    return resp.pop(0)
                return resp
        return 404, "{}"

    return http, state


def test_live_complete_success_json(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({
        "choices": [{"message": {"content": '{"result":"PASS"}'}}],
        "usage": {"total_tokens": 10},
    })
    http, state = _fake_http_factory({"/chat/completions": (200, body)})
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=http)
    res = r.complete("judge", [{"role": "user", "content": "x"}], output_schema={"type": "object"})
    assert res.ok is True
    assert res.parsed_json == {"result": "PASS"}
    assert state["calls"][0]["key"] == "sk-test"
    assert state["calls"][0]["payload"]["response_format"] == {"type": "json_object"}
    assert r.call_log()[0]["ok"] is True


def test_deepseek_thinking_can_be_disabled_for_deterministic_rag(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({
        "choices": [{"message": {"content": '{"result":"PASS"}'}}],
        "usage": {},
    })
    http, state = _fake_http_factory({"/chat/completions": (200, body)})
    cfg = RouterConfig(
        live=True,
        chat_primary=ModelConfig(
            "deepseek-flash", "chat", thinking_mode="disabled",
        ),
    )

    result = ModelRouter(cfg, http=http).complete(
        "rag", [{"role": "user", "content": "JSON"}],
        output_schema={"type": "object"},
    )

    assert result.ok is True
    assert state["calls"][0]["payload"]["thinking"] == {"type": "disabled"}


def test_structured_output_without_gateway_json_mode(monkeypatch):
    """Reasoning models may support JSON text but reject response_format."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({
        "choices": [{"message": {"content": '```json\n{"result":"PASS"}\n```'}}],
        "usage": {"total_tokens": 10},
    })
    http, state = _fake_http_factory({"/chat/completions": (200, body)})
    cfg = RouterConfig(
        live=True,
        chat_primary=ModelConfig(
            "glm-5.2-107", "chat", retries=0, supports_json_mode=False,
        ),
        chat_fallbacks=(),
    )
    router = ModelRouter(cfg, http=http)

    result = router.complete(
        "judge",
        [{"role": "user", "content": "只返回 JSON"}],
        output_schema={"type": "object"},
    )

    assert result.ok is True
    assert result.parsed_json == {"result": "PASS"}
    assert "response_format" not in state["calls"][0]["payload"]


def test_live_complete_falls_back_to_second_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    # Primary with no retries: one 500 → immediately fall through to secondary.
    primary = ModelConfig("unavailable-model", "chat", retries=0)
    secondary_body = json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {}})
    responses = {"/chat/completions": [(500, '{"error":"boom"}'), (200, secondary_body)]}
    http, state = _fake_http_factory(responses)
    cfg = RouterConfig(
        live=True, chat_primary=primary,
        chat_fallbacks=(ModelConfig("deepseek-flash", "chat", retries=0),),
    )
    r = ModelRouter(cfg, http=http)
    res = r.complete("t", [{"role": "user", "content": "x"}])
    assert res.ok is True
    assert res.model == "deepseek-flash"
    assert len(state["calls"]) == 2


def test_live_complete_all_fail_returns_degradation(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    http, _ = _fake_http_factory({"/chat/completions": (500, '{"error":"x"}')})
    cfg = RouterConfig(
        live=True,
        reranker=ModelConfig("dedicated-reranker", "rerank"),
        rerank_via_chat=False,
    )
    r = ModelRouter(cfg, http=http)
    res = r.complete("t", [{"role": "user", "content": "x"}])
    assert res.ok is False
    assert res.fallback is True
    assert res.model == "all-failed"


def test_duplicate_fallback_model_is_not_called_twice(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    http, state = _fake_http_factory({"/chat/completions": (500, '{"error":"x"}')})
    same = ModelConfig("same-model", "chat", retries=0)
    router = ModelRouter(
        RouterConfig(live=True, chat_primary=same, chat_fallbacks=(same,)),
        http=http,
    )

    result = router.complete("t", [{"role": "user", "content": "x"}])

    assert result.ok is False
    assert len(state["calls"]) == 1


def test_empty_chat_content_is_logged_as_failure(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({
        "choices": [{"message": {"content": None, "reasoning_content": "thinking"}}],
        "usage": {"total_tokens": 10},
    })
    http, _ = _fake_http_factory({"/chat/completions": (200, body)})
    cfg = RouterConfig(
        live=True,
        chat_primary=ModelConfig("only-model", "chat", retries=0),
        chat_fallbacks=(),
    )
    r = ModelRouter(cfg, http=http)

    res = r.complete("t", [{"role": "user", "content": "x"}])

    assert res.ok is False
    assert r.call_log()[0]["ok"] is False
    assert "empty content" in r.call_log()[0]["error"]


def test_live_embed_parses_dimension(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({"data": [{"embedding": [0.1, 0.2, 0.3]}], "usage": {}})
    http, _ = _fake_http_factory({"/embeddings": (200, body)})
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=http)
    res = r.embed(["text"])
    assert res.ok is True
    assert res.dim == 3
    assert res.vectors == [[0.1, 0.2, 0.3]]


def test_live_embed_falls_back_to_offline_on_error(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    http, _ = _fake_http_factory({"/embeddings": (500, '{"error":"x"}')})
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=http)
    res = r.embed(["text"])
    assert res.ok is True
    assert res.fallback is True
    assert res.model == "offline-hash-256"
    assert res.dim == 256


def test_live_rerank_parses_scores(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({"results": [
        {"index": 0, "relevance_score": 0.9},
        {"index": 1, "relevance_score": 0.4},
    ]})
    http, _ = _fake_http_factory({"/rerank": (200, body)})
    cfg = RouterConfig(
        live=True,
        reranker=ModelConfig("dedicated-reranker", "rerank"),
        rerank_via_chat=False,
    )
    r = ModelRouter(cfg, http=http)
    res = r.rerank("q", ["a", "b"])
    assert res.ok is True
    assert res.scores == [0.9, 0.4]


def test_deepseek_chat_rerank_parses_and_clamps_scores(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({
        "choices": [{"message": {"content": '{"scores":[1.2,-0.1]}'}}],
        "usage": {},
    })
    http, state = _fake_http_factory({"/chat/completions": (200, body)})
    router = ModelRouter(
        RouterConfig(live=True, embedding_enabled=False, rerank_via_chat=True),
        http=http,
    )

    result = router.rerank("什么是栈", ["A stack is LIFO.", "A tree has nodes."])

    assert result.ok is True
    assert result.scores == [1.0, 0.0]
    assert state["calls"][0]["url"].endswith("/chat/completions")


def test_deepseek_expands_chinese_query_for_english_textbook(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = json.dumps({
        "choices": [{"message": {"content": (
            '{"queries":["binary search tree", "BST", "binary search tree"]}'
        )}}],
        "usage": {},
    })
    http, _ = _fake_http_factory({"/chat/completions": (200, body)})
    router = ModelRouter(RouterConfig(live=True), http=http)

    result = router.expand_query("什么是二叉搜索树？")

    assert result == ["binary search tree", "BST"]


def test_healthcheck_does_not_report_offline_embedding_as_healthy(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    chat_body = json.dumps({"choices": [{"message": {"content": "pong"}}], "usage": {}})
    rerank_body = json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]})
    http, state = _fake_http_factory({
        "/chat/completions": (200, chat_body),
        "/embeddings": (500, '{"error":"x"}'),
        "/rerank": (200, rerank_body),
    })
    r = ModelRouter(RouterConfig(
        live=True,
        reranker=ModelConfig("dedicated-reranker", "rerank"),
        rerank_via_chat=False,
    ), http=http)

    health = r.healthcheck()

    assert health["chat"] is True
    assert health["embedding"] is False
    assert health["embedding_fallback"] is True
    assert health["reranker"] is True
    assert health["healthy"] is False
    chat_call = next(c for c in state["calls"] if c["url"].endswith("/chat/completions"))
    assert chat_call["payload"]["max_tokens"] == 4096


# --- JSON extraction edge cases ------------------------------------------

def test_extract_json_direct():
    assert _maybe_extract_json('{"a": 1}', True) == {"a": 1}


def test_extract_json_in_prose():
    assert _maybe_extract_json('The answer is {"a": 1} done.', True) == {"a": 1}


def test_extract_json_fenced():
    txt = '```json\n{"a": 1}\n```'
    assert _maybe_extract_json(txt, True) == {"a": 1}


def test_extract_json_none_when_not_wanted():
    assert _maybe_extract_json('{"a": 1}', False) is None


# --- tokenizer -----------------------------------------------------------

def test_tokenize_mixed_cjk_and_ascii():
    toks = set(_tokenize("Java 中 == 和 equals 的区别"))
    assert "java" in toks
    assert "equals" in toks
    # Isolated CJK characters stay searchable, while phrases use n-grams so
    # ubiquitous single characters do not swamp textbook relevance scores.
    assert "中" in toks
    assert "区别" in toks
    assert "的区别" in toks


def test_hash_embedding_stable():
    a = _hash_embedding("hello world", 64)
    b = _hash_embedding("hello world", 64)
    assert a == b
