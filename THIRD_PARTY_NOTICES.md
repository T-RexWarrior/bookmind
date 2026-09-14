# THIRD-PARTY NOTICES — 学迹 / BookMind

This project references the design and, where licenses permit, specific
infrastructure of several open-source projects. The core learning engine
(Evidence Ledger, L0–L4 Evidence Gate, Misconception Hypothesis, Diagnostic
Probe, Remediation/Reverification, Next Best Action, long-term learner state)
is original to BookMind and is not derived from any of the projects below.

Per OPEN_SOURCE_REFERENCES.md, every entry's version must be pinned to a
specific tag/commit before release. Licenses marked "待复核" (to be confirmed)
mean the project is used as a **design reference only** — no code is copied
until the license is verified.

## Runtime dependencies (Python)

| Package | License | Use |
|---|---|---|
| FastAPI | MIT | HTTP API framework |
| Pydantic | MIT | Domain schema validation |
| pydantic-settings | MIT | Configuration from env / .env (M0) |
| Uvicorn | BSD-3-Clause | ASGI server |
| httpx | BSD-3-Clause | Live-model HTTP transport (optional) |
| SQLAlchemy | MIT | ORM / persistence layer (M1, SQLite + PostgreSQL) |
| Alembic | MIT | Database migrations (M1) |
| pypdf | BSD-3-Clause | Fast text-layer extraction for standard PDFs |
| RapidOCR | Apache-2.0 | Built-in local Chinese/English OCR fallback |
| ONNX Runtime | MIT | Local inference runtime for RapidOCR models |
| pypdfium2 | Apache-2.0 OR BSD-3-Clause, plus PDFium third-party notices | Render scanned PDF pages for OCR |
| pytest | MIT | Test runner |

## Runtime dependencies (Frontend, M5)

The product UI is a React + Vite + TypeScript app (PRODUCTIZATION §7.1).
Versions are pinned in `frontend/package-lock.json`; the built `frontend/dist/`
is committed so the competition venue runs with no Node toolchain. The
shadcn/ui-style primitives in `frontend/src/components/ui/` are locally
maintained source (design approach borrowed, not a package import).

| Package | License | Use |
|---|---|---|
| react / react-dom | MIT | UI runtime (pinned v19) |
| react-router-dom | MIT | Client-side routing |
| @assistant-ui/react | MIT | Chat primitives (Thread/Composer/MessageList, external-store runtime) |
| react-pdf | MIT | PDF Reader (text layer + locally-bundled worker, offline-safe) |
| vite | MIT | Build tool / dev server |
| typescript | Apache-2.0 | Type checking |
| @vitejs/plugin-react | MIT | Vite React plugin |
| tailwindcss | MIT | Styling |

## Design references (no code copied unless noted)

| Project | License | Use | Not adopted |
|---|---|---|---|
| [Oppia](https://github.com/oppia/oppia) | Apache-2.0 (待复核 commit) | Bug Library, Specific Error, Remediation, staged-question design | Platform code & content system |
| [adaptive-learning-agent](https://github.com/abhijeetgupta23/ai-agent-to-learn-faster-and-better) | MIT (待复核) | GoldenCase, trace, fixed-flow baseline, prerequisite-blocking case | LLM taking over deterministic state; triple-judge as sole evidence |
| [OpenMAIC](https://github.com/THU-MAIC/OpenMAIC) | MIT root (bundled packages 分项复核) | Streaming events, parser provider, local interaction components | Classroom/slides/voice/whiteboard system; LLM Director |
| [MinerU](https://github.com/opendatalab/MinerU) | Apache-2.0 + 附加条款 (待复核) | PDF parsing, structured artifacts, page/layout debug | Copying its internal parser implementation |
| [GenT / Agentic-AI-Tutor](https://github.com/Rehab-Hamdy/Agentic-AI-Tutor) | **未确认 — design reference only, no code copied** | Hybrid RAG ordering, Citation Schema, task quality gate | Code, assets, prompts, derived implementations; Neo4j/.NET backends |
| [AI Exam Assistant](https://github.com/Nambu89/AI_Exam_Assistant) | MIT (待复核 commit) | Offline demo corpus, evaluation gate, credential-free run | Azure/Microsoft Agent Framework binding |

## LLM gateway

The live-model path uses the USTC campus LLM gateway
(`https://api.llm.ustc.edu.cn/v1`), an OpenAI-compatible endpoint. No model
weights are redistributed; the gateway is called at runtime when
`USTC_LLM_API_KEY` is set, and the system falls back to deterministic offline
mode when it is unset or any model call fails.

## Attribution display

The UI displays this attribution in the project introduction. The "FSRS-inspired"
review scheduling is a simplified, uncalibrated curve and is not claimed to be
full FSRS or calibrated on real students (LEARNING_MODEL §6, EVALUATION §9).
