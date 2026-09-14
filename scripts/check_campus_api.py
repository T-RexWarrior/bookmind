"""Safe connectivity check for the USTC OpenAI-compatible gateway.

The API key is read from USTC_LLM_API_KEY and is never printed or persisted.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from bookmind.api.dependencies import _router_config_from_settings
from bookmind.config import Settings
from bookmind.llm.router import ModelRouter


def main() -> int:
    key = os.environ.get("USTC_LLM_API_KEY")
    if not key:
        print(json.dumps({"ok": False, "error": "USTC_LLM_API_KEY is not set"}))
        return 2

    settings = Settings()
    model_check = _list_models(settings.llm_base_url, key)
    router = ModelRouter(_router_config_from_settings(settings, live=True))
    chat = router.complete(
        "kg_connectivity",
        [
            {
                "role": "system",
                "content": (
                    "Extract concepts from textbook sentences. Return only a JSON object "
                    'with this shape: {"concepts":[{"name":"..."}]}.'
                ),
            },
            {
                "role": "user",
                "content": (
                    "Gradient descent minimizes a loss function. "
                    "Backpropagation computes gradients in a neural network."
                ),
            },
        ],
        output_schema={"type": "object"},
        max_tokens=4096,
        temperature=0.0,
    )
    embedding = router.embed(["gradient descent", "backpropagation"])
    reranker = router.rerank(
        "How are neural-network gradients computed?",
        [
            "Backpropagation applies the chain rule to compute gradients.",
            "Gradient descent updates parameters.",
        ],
    )
    result = {
        "models": model_check,
        "chat": {
            "ok": chat.ok,
            "model": chat.model,
            "structured_json": isinstance(chat.parsed_json, dict),
            "error": chat.error,
        },
        "embedding": {
            "ok": embedding.ok,
            "model": embedding.model,
            "dimension": embedding.dim,
            "fallback": embedding.fallback,
        },
        "reranker": {
            "ok": reranker.ok,
            "model": reranker.model,
            "score_count": len(reranker.scores),
            "fallback": reranker.fallback,
        },
        "attempts": [
            {
                "task": item["task"],
                "model": item["model"],
                "ok": item["ok"],
                "error": item["error"][:180],
            }
            for item in router.call_log()
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if model_check["ok"] and chat.ok and not embedding.fallback else 1


def _list_models(base_url: str, key: str) -> dict:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    required = {
        "glm-5.2-107",
        "deepseek-v4-pro",
        "qwen-chat",
        "qwen3-embedding",
        "qwen3-reranker",
    }
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
            model_ids = {item.get("id") for item in body.get("data", [])}
            return {
                "ok": response.status == 200,
                "status": response.status,
                "count": len(model_ids),
                "configured_models_present": required.issubset(model_ids),
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "count": 0, "configured_models_present": False}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": None,
            "count": 0,
            "configured_models_present": False,
            "error": type(exc).__name__,
        }


if __name__ == "__main__":
    sys.exit(main())
