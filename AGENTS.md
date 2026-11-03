# AGENTS.md — Enterprise Agent

Compact guidance for future OpenCode sessions. Only repo-specific, hard-to-infer facts.

## What this repo is
- Self-built Enterprise AI Operations Agent (Python 3.13+, FastAPI, DeepSeek via OpenAI-compatible API).
- Architecture: `FastAPI Gateway (:8761) → Agent Runtime (Planner/Executor loop) → Tool Registry → MCP/DB/RAG → Policy/HITL → Workflow → Eval/Obs`.
- Strategy: hand-rolled Runtime first (TDD), then LangGraph parity check at Stage 8.
- Three target scenarios (in order): IT Ops → Contract Compliance → Sales Analytics. Operates against the sibling `scm-platform` repo.

## Setup & run (Windows, PowerShell)
```powershell
cd D:\ProgramProject\scm-platform\enterprise-agent
py -m venv .venv
.\.venv\Scripts\activate
pip install -e .         # optional extras: .[mcp] .[observability] .[langgraph]
copy .env.example .env   # fill DEEPSEEK_API_KEY (real or sk-xxxx for echo fallback)
pytest -q                # 183 passed, 8 skipped (bare install; 187/6 with .[observability,langgraph,mcp])
uvicorn apps.api.main:app --port 8762 --reload
# DeepSeek connectivity sanity check:
python scripts/check_deepseek.py
```
- `.env` is git-ignored; never commit `DEEPSEEK_API_KEY` or any real key (PR template explicitly checks this).
- Without a real key the runtime degrades to echo (`[runtime-echo] <task>`) — see `agent/runtime/llm_planner.py:14` — so tests still pass offline.

## TDD discipline (REQUIRED)
This repo is built RED → GREEN per stage. Before any implementation:
1. Write a failing test in `tests/test_<stage>.py` (file naming reflects stage: `test_health`, `test_runtime_loop`, `test_planner_llm`, `test_tools`, `test_knowledge`, `test_memory`, `test_policy`, `test_workflow`, `test_eval_obs`, `test_stage8`).
2. Run `pytest -q` to confirm RED.
3. Implement the minimum to go GREEN; refactor only after green.
- PR template enforces: `pytest -q` green + RED-first tests + no secrets.
- `pytest-asyncio` is `asyncio_mode = "auto"` — plain `def test_` works for async too.

## Package layout (single installable, top-level packages)
- `apps/api/main.py` — FastAPI gateway; `/health`, `/metrics`, `/chat`, `/approval/{aid}/decide`, `/chat/{task_id}/resume`.
- `agent/runtime/` — `state.py` (AgentState), `loop.py` (Planner→Executor→Observation→Reflection, max 8), `planner.py` (defaults), `llm_planner.py` (DeepSeek + JSON parser), `paused_tasks.py` (InMemory/Postgres paused state).
- `agent/tools/` — `registry.py` (in-memory Tool catalog), `executor.py`, `builtin.py`.
- `agent/mcp/server.py` — optional MCP server for metrics/logs/git (requires `.[mcp]`).
- `agent/memory/` — `store.py` (five layers) and `episodic.py` (task transitions + similarity recall).
- `knowledge/` — `document.py`, `ingestion.py` (chunking with metadata), `store.py` (filter → vector recall → rerank), `embedding.py`, `vector_index.py`, `pgvector_store.py` (opt-in), `seed.py`.
- `security/` — `policy.py` (PolicyEngine) and `approval.py` (ApprovalStore + InMemory/Postgres repositories).
- `workflow/` — `graph.py` (hand-rolled), `nodes/expense.py`, `graph_lang.py` (real StateGraph when langgraph is installed), `checkpoint.py` (memory/PG saver), `scenarios.py` (contract + sales analyzers), `data/` fixtures.
- `observability/` — `tracing.py` (in-memory Tracer), `metrics.py` (Prometheus, optional), `otel.py` (OTel adapter, optional).
- `evaluation/evaluator.py` — case-based evaluator.
- `scripts/db/agent_schema.sql` — `agent_approval` / `agent_paused_task` DDL for `db_agent`.

## Cross-cutting invariants (do not break)
- **Planner JSON contract** (`agent/runtime/llm_planner.py:6`): LLM must only emit `{"action":"finish",...}` or `{"action":"call_tool","tool":...,"args":...}`. Anything else → fallback to `finish` with raw text. This is also the prompt-injection defense per `SECURITY.md`.
- **Tenant/permission filtering** in `knowledge/store.py` and `security/policy.py` must never be bypassable. Chunks always carry `tenant_id / department / permission / version`.
- **Policy check before tool execution** — LLM never calls tools directly; `guarded_executor` + `PolicyEngine` are the enforcement points.
- **HITL approvals are single-use and exact-match.** `approved_approval_id` only opens the gate when the stored approval is `approved`, unconsumed, and its `tool` + `args` equal the pending action. `/chat/{task_id}/resume` executes that one action deterministically. Persistence is repository-based: InMemory by default, Postgres when `AGENT_PG_DSN` is set; do not add process-local dicts outside the repository.
- **LangGraph is optional, not guaranteed.** `workflow/graph_lang.py` uses a real `StateGraph` only when `langgraph` is installed **and** the checkpointer is native; otherwise it runs the honest simulation path. `PostgresSaver` needs `.[langgraph]` plus `AGENT_PG_DSN`.
- **Optional integrations degrade.** `mcp`, `prometheus-client`, `opentelemetry-*`, `redis`, and `tiktoken` are optional groups; missing ones must skip or return an explicit 503/None, never fail the suite.

## Stage roadmap (see `docs/superpowers/plans/2026-09-15-enterprise-agent-roadmap.md`)
All eight stages are implemented under TDD; optional pieces stay optional.
- Stage 2 MCP: stub is real (`agent/mcp`), server needs `.[mcp]`.
- Stage 3 RAG: local hashing embedding + vector index/rerank are always on; pgvector is opt-in and needs the DB extension.
- Stage 4 Memory: Episodic is implemented (transitions + similarity recall), PG-backed only with a DSN.
- Stage 5 HITL: approvals/paused tasks have InMemory + Postgres repositories.
- Stage 7 Obs: memory Tracer always on; Prometheus `/metrics` and OTel spans need `.[observability]`.
- Stage 8: real LangGraph graph when `.[langgraph]` is installed; PG checkpoint needs a DSN; contract/sales scenarios are fixture-driven.
- Remote Postgres lives at `192.168.0.121:5432`; credentials come from the environment (`PGUSER`/`PGPASSWORD` or an injected `AGENT_PG_DSN`), never from repo files. Agent-owned tables use the dedicated `db_agent` database, initialised with `scripts/db/agent_schema.sql`; never point agent tables at a business `db_*` database.
- `AGENT_PG_DSN` is the **only** switch for approvals, paused tasks, episodic memory and checkpoints. `PG_DSN`/`POSTGRES_DSN` stay legacy MemoryStore settings. No DSN ⇒ InMemory/no external DB.
- Integration tests are opt-in: `AGENT_TEST_PG_DSN=postgresql://<user>:<password>@192.168.0.121:5432/db_agent pytest -q`. Without it, the PG/pgvector tests skip and the offline suite stays green.
- Spec/plan source of truth: `docs/superpowers/specs/2026-09-15-enterprise-agent-design.md` and the matching `plans/` file. Trust those over README prose.

## Things easy to miss
- `.idea/` is local PyCharm state (partially git-ignored via `.idea/.gitignore`); ignore it.
- `enterprise_agent.egg-info/`, `.pytest_cache/`, `.venv/`, `__pycache__/` are all in `.gitignore`.
- `.superpowers/` is git-ignored (do not commit superpowers skill cache).
- CI runs on Python 3.12 and 3.13 via `.github/workflows/ci.yml` — only `pip install -e .` then `pytest -q`. No lint/typecheck steps configured yet.
- `tests/test_health.py::test_chat_echo` and `tests/test_planner_llm.py::test_chat_goes_through_runtime` depend on echo-fallback when no key; they are not integration tests against DeepSeek.

## Where to look first when investigating
1. `pyproject.toml` + `.env.example` — deps, Python version, required env.
2. `apps/api/main.py` — real entrypoint and wire-up.
3. `agent/runtime/loop.py` + `state.py` — runtime contract used by every scenario.
4. `docs/superpowers/plans/2026-09-15-enterprise-agent-roadmap.md` — current stage + next steps.
5. `SECURITY.md` — security model and reporting channel (GitHub private vulnerability reporting, not public issues).