"""LLM gateway — ModelRouter, schemas and prompts.

ARCHITECTURE.md §8: a single interface hiding model differences::

    complete(task, messages, output_schema=None) -> ModelResult
    embed(texts) -> list[vector]
    rerank(query, passages) -> scores

The router connects to the USTC campus gateway (OpenAI-compatible) for the
real path and falls back to deterministic offline fixtures for the demo/CI
path. Per the spec, "实时模型作为可切换增强，而不是比赛主路径的单点依赖":
the offline engine keeps working with no network, and a live model is an
opt-in enhancement.
"""
