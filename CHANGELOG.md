# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version numbers are cut from the roadmap, not derived automatically, so
0.1.0 → 0.2.0 is not "one more release" — read the entries.

## [0.5.2] — 2026-11-03

Patch release: documentation verification boundaries plus two regression tests.

### Documentation

- README (EN/ZH) now separates **CI** coverage from **manual** verification in
  the conditional-capability table, so fixture tests are not presented as
  real-environment validation.
- Roadmap records v0.5.0/v0.5.1 as released, marks the OTLP exporter and the
  pgvector `5434` instance as manually verified, and updates the test matrix to
  include `193 passed, 0 skipped` against `db_agent_vector`.
- AGENTS.md corrects the supported Python range to 3.12/3.13, the gateway port
  to 8762, and the test-count matrix; it also states the CI/manual boundary.

### Testing

- Pin the API 500 response so an unexpected exception cannot echo
  `str(exc)` to the caller.
- Cover explicit OTLP protocol overrides (`grpc`, `http/protobuf`, `http`) and
  unknown-value fallback to the port heuristic, plus `grpc://` normalization.
- Bare install is now `185 passed, 8 skipped`; with extras `189 passed, 6 skipped`.
  The PG/pgvector rows remain the last manual measurements (192/1 and 193/0
  before these two tests) and are labelled as such in the README.

### Known limitations

- MCP is stdio-only (child process), not a resident HTTP service.
- OTLP spans reach a collector, but no vendor backend, alerting or retention
  policy is configured.
- PG/pgvector integration cases skip when `AGENT_TEST_PG_DSN` is absent.
- No kill/restart drill has been run; the PG checkpoint test only proves a new
  saver instance can resume, not a new process.

## [0.5.1] — 2026-11-03

### Added

- Optional `opentelemetry-exporter-otlp` in the `observability` group. A real
  OTLP span exporter is created only when both the package and
  `OTEL_EXPORTER_OTLP_ENDPOINT` are present; otherwise the in-memory Tracer
  remains active and the chosen path is logged. gRPC (4317) and HTTP (4318)
  endpoints are selected by port or `OTEL_EXPORTER_OTLP_PROTOCOL`.
- `python -m agent.mcp.server` starts the MCP server on stdio. MCP is
  intentionally child-process only; no HTTP daemon or long-running service is
  claimed.
- Real pgvector verification against `192.168.0.121:5434/db_agent_vector`
  (`vector 0.8.7`): `tests/test_rag.py` = 7 passed, full suite = 193 passed,
  0 skipped. Port **5433** is not used; it belongs to another project.

### Changed — dependency refresh

Runtime core (verified with 187 passed / 6 skipped after each batch):

| Package | Before | After |
| --- | --- | --- |
| fastapi | 0.141.1 | 0.162.0 |
| uvicorn[standard] | 0.53.0 | 0.54.0 |
| pydantic | 2.13.5 | 2.14.2 |
| pydantic-settings | 2.15.0 | 2.16.2 |
| openai | 3.14.0 | 3.42.1 |
| sqlalchemy | 2.0.54 | 2.1.6 |
| psycopg[binary] | 3.3.5 | 3.3.7 |
| starlette (transitive) | 1.6.0 | 1.10.0 |
| anyio (transitive) | 4.15.1 | 4.16.0 |

Optional/tooling:

| Package | Before | After |
| --- | --- | --- |
| mcp | 2.3.0 | 2.3.0 (already latest) |
| prometheus-client | 0.26.0 | 0.26.0 (already latest) |
| opentelemetry-api/sdk/exporter | 1.45.2 | 1.45.2 (already latest) |
| langgraph | 1.2.19 | 1.2.19 (already latest) |
| langgraph-checkpoint-postgres | 3.2.0 | 3.2.0 (already latest) |
| langchain-core (transitive) | 1.7.3 | 1.7.4 |
| redis | not installed | 8.1.0 |
| tiktoken | not installed | 0.14.0 |
| pytest | 9.1.1 | 9.2.0 |
| pytest-asyncio | 1.4.0 | 1.4.1.post1 |

Held:

- `uuid_utils` stays at **0.19.0** (latest 1.2.0) because `langsmith 0.14.17`
  requires `uuid-utils<1.0,>=0.12.0`; upgrading it breaks the LangGraph stack.
- `pip` itself stays at 25.3 (26.3 available); it is tooling, not a project
  dependency, and is not part of the application environment contract.

## [0.5.0] — 2026-11-03

### Added

**HITL round trip**

- `POST /approval/{approval_id}/decide` records an approve/reject decision
  (404 unknown, 403 cross-tenant, 409 already decided).
  `POST /chat/{task_id}/resume` then executes the single approved action and
  closes the task, or cancels it when the approval was rejected.
- `ApprovalStore.consume` marks an approval single-use, and
  `guarded_executor(..., approved_approval_id=...)` only lets that one matching
  `tool` + `args` pair through the policy gate.

**Episodic memory**

- `agent/memory/episodic.py`: every `AgentTask` status transition is written to
  `memory_episodic`. Cross-task recall scores by task type, tool names and
  2-gram/keyword overlap, with tenant isolation. Uses the existing connector
  when configured; otherwise an in-process list keeps offline tests working.
- `EpisodicMemoryProvider` is now part of the default context providers and
  formats recalled episodes as "类似历史任务".

**Durable approval / paused-task state**

- `security/approval.py` now has an `ApprovalRepository` interface with
  InMemory and Postgres implementations (`agent_approval`).
- `agent/runtime/paused_tasks.py` persists the whole `AgentState` (including
  the `AgentTask` snapshot) per `(tenant_id, task_id)` in `agent_paused_task`.
- New `scripts/db/agent_schema.sql`; the DDL is also idempotently applied by
  `infrastructure/pg/schema.py`.
- No `AGENT_PG_DSN` ⇒ automatic in-memory fallback, so local development and CI
  never need a database. `PG_DSN`/`POSTGRES_DSN` remain legacy MemoryStore
  configuration and do **not** activate agent persistence.

**MCP server stub**

- `agent/mcp/server.py` exposes `metrics.get`, `logs.search` and `git.diff`
  through the existing Tool Registry, with MCP-shaped content results.
- `mcp` is an optional dependency group (`pip install -e .[mcp]`). Without it,
  the pure spec/call helpers still work; `build_server` raises
  `MCPUnavailable` instead of crashing the package import.

**RAG embedding / vector retrieval / rerank**

- `knowledge/embedding.py`: deterministic local feature-hashing embeddings,
  no model or network required.
- `knowledge/vector_index.py`: in-memory cosine index plus weighted lexical +
  vector rerank.
- `knowledge/store.py` now applies tenant/permission filtering **before**
  vector recall and then reranks; the old lexical behaviour stays available.
- Optional `knowledge/pgvector_store.py` behind `RAG_PGVECTOR_ENABLED=1` and a
  DSN. It falls back to the in-memory store when the extension is absent.

**Observability**

- `GET /metrics` with `prometheus-client` (optional group `observability`).
  Without the package it returns 503 and a message instead of failing startup.
- `observability/otel.py` adapts trace events to OpenTelemetry spans when the
  SDK is installed; the in-memory `Tracer` remains the fallback.

**LangGraph real graph**

- `workflow/checkpoint.py` handles the context-manager `PostgresSaver`
  (`from_conn_string`), adds a connect timeout and closes the connection.
- Resume uses `Command(resume=...)` on LangGraph 1.x and survives the
  `__interrupt__` value that `PostgresSaver` writes into `__root__`.
- Real `StateGraph` tests cover the auto path, pause/resume with a memory
  saver, and PG checkpoint write/read/resume with `AGENT_TEST_PG_DSN`.

**Scenario implementations**

- `review_contract` accepts a dict or a JSON file, validates required fields,
  produces structured `findings` (risky clause / amount threshold), retrieves
  policy from the knowledge base and calls `scm.supplier.get` only when a
  supplier id is supplied.
- `analyze_sales` aggregates the repo fixture `workflow/data/sales_week.json`
  (GMV, orders, AOV, failed payments, per-category change), adds metrics and
  knowledge tool calls, and returns a structured report. Missing/empty data
  returns `insufficient_data` instead of inventing numbers.

### Changed

- `PGConnector` normalises `postgresql://` DSNs to the installed psycopg 3
  driver and translates `?` placeholders to SQLAlchemy named parameters, so
  real-Postgres reads/writes work instead of only the in-memory connector.
- `ApprovalStore` and `PausedTaskStore` are module-level singletons in
  `apps/api/main.py`, backed by repositories rather than raw dicts.
- `AgentTask.to_dict()` now includes `created_at`/`updated_at` so paused tasks
  can be reconstructed after a restart.

### Quality

- Bare `pip install -e .`: **183 passed, 8 skipped**. The skips are the
  real-Postgres/pgvector integration tests (`skipif` on `AGENT_TEST_PG_DSN` /
  extension availability) plus optional-dependency probes (`langgraph` module,
  `mcp`, and `prometheus-client`).
- With the optional `.[observability,langgraph,mcp]` extras (no DSN):
  **187 passed, 6 skipped** — only the DSN/pgvector integration tests skip.
- With `AGENT_TEST_PG_DSN=postgresql://<user>:<password>@192.168.0.121:5432/db_agent`:
  **192 passed, 1 skipped** — the only skip is pgvector because the VM's
  Postgres image has no `vector` extension.
- New test files: `test_episodic.py`, `test_approval_persistence.py`,
  `test_agent_pg_integration.py`, `test_mcp_server.py`, `test_rag.py`,
  `test_observability_optional.py`, `test_langgraph_real.py`,
  `test_scenarios_real.py`.

### Activation conditions (not "production ready")

- Postgres approval/paused state: `AGENT_PG_DSN` to `db_agent` + schema SQL.
- MCP server: install the optional `mcp` group.
- Prometheus: install `prometheus-client`; OTel: install `opentelemetry-*`.
- LangGraph: install the optional `langgraph` group; PG checkpoint additionally
  needs `langgraph-checkpoint-postgres` and a DSN.
- pgvector: install the `vector` extension in the database and set
  `RAG_PGVECTOR_ENABLED=1`.
- Real SCM scenario data: set `SCM_GATEWAY_URL`; without it the tools keep their
  documented mock behaviour.

### Still deferred

- Multi-process approval/paused state is only as durable as the configured
  Postgres; no Redis/Kafka outbox has been added.
- OTLP exporter/collector deployment is not included.
- pgvector integration test cannot pass on the current VM until the extension is
  installed; the fallback path is covered.
- A full "kill the process, start a new process, resume" test still needs a
  separate harness; the new-saver-instance resume is tested.

## [0.4.1] — 2026-09-18

SCM Integration v2 plus the closed HITL loop. Closes seven gaps from the
v0.4.0 roadmap against a live scm-platform backend.

### Added

- **Write tools registered.** `scm.order.cancel` and `scm.stock.adjust` join
  `scm.purchase.create`. All three are placeholders that return a
  `"need approval: …"` result; registering them stops the planner from
  emitting a name the registry has never seen.
- **Retry on 5xx / `ConnectError` / `Timeout`.** `ScmClient.get` retries once
  with configurable backoff (`SCM_RETRY_BACKOFF_SECONDS`, default 0.3 s) and
  never raises.
- **`run_it_ops` scenario.** `workflow.scenarios` stitches `metrics.get`,
  `logs.search`, and `scm.order.get` into an IT-operations diagnostic path —
  the first of the three roadmap scenarios.
- **`/chat` surfaces `approval_id`.** The payload now exposes `approval_id`,
  `approval_tool`, and `approval_args` when the Runtime ends paused.
- **`scm.supplier.get` plus the `review_contract` supplier hook.** Real
  supplier risk data reaches contract review.
- **`scm_call` / `scm_result` trace events.** A `contextvar`-based hook fires
  whenever `scm_trace_id` is set by `/chat`.
- **`tenant_map` validation.** Bad JSON, an empty dict, or an unknown tenant
  now warns and falls back instead of dropping silently.

### Fixed

A final code review caught one **blocking** defect: the production `/chat`
path never reached `guarded_executor`, so the policy gate was inert in the
deployed gateway even though unit tests passed. Fixed by injecting the
executor as a lambda, plus one real integration test.

### Quality

- 3 PRs (#17, #18, #19), 28 new tests, 128 passing on Python 3.12 and 3.13.

### Deferred to v3

- `/approval/{aid}/decide` HTTP endpoint
- `ApprovalStore` persistence to Postgres
- `/chat/{task_id}/resume` from a paused state
- LangGraph refactor

## [0.4.0] — 2026-09-16

`AgentTask`, `Counter`, and `Pricing`.

### Added

- `AgentTask` dataclass with a uuid4 id and a state machine over
  pending / running / paused / done / failed / cancelled, a 50-entry history
  cap, an invalid-transition guard, and `attach_checkpoint`.
- `Counter` (tiktoken-first, heuristic fallback) and `Pricing`
  (`deepseek-chat` defaults, env-overridable).
- The loop auto-wraps `AgentState` → `AgentTask`, records tokens each round,
  and emits `task_status_changed` trace events.
- Planner and executor exceptions now produce a `failed` transition, so the
  state machine always converges.

### Changed

- `/chat` exposes `task_id`, `token_usage`, and `cost`.

### Quality

- 91 tests passed (78 baseline + 13 new); CI green on 3.12 and 3.13.

### Known follow-ups

- `Tracer._shared` thread safety, before async arrives
- `AgentState.status` redundancy against `AgentTask.status` (Stage 8 refactor)
- `AgentTask` auth and identity
- Carried over from v0.2.0: real-PG kill-process restore across restarts

## [0.3.0] — 2026-09-16

Context engineering and the five-layer memory model.

### Added

**Context — nine-layer assembly, budget, compression**

- Tiktoken-accurate counting with a heuristic fallback; `TokenBudget` with
  priority-based dropping; system and task layers are never dropped.
- Six base providers (System, Task, Conversation, Observation, RAG,
  ToolTrace), three memory providers (User, Org, Conversation), one
  placeholder (Episodic).
- Dependency-free extractive summarisation, triggered above 0.8 × available,
  keeping the most recent turns per layer; the original text is preserved in
  state for the durable path.
- RAG score tiering with a 40–60 clamp, NaN/inf hardening, and unique ids for
  same-named chunks so a dedup pass cannot delete the wrong one.
- Loop wiring: the planner receives the assembled window plus a defensive
  copy; executor and state see the full set.
- `context_quality_gate` asserts usage ≤ 0.7 × baseline with an unchanged
  decision.

**Memory — five interfaces, Postgres primary, in-memory fallback**

- `infrastructure/pg/connector.py`: `InMemoryConnector` for dev and CI plus
  `PGConnector` (pool size 5, `pool_pre_ping`, retries at 1 s / 2 s / 4 s,
  healthcheck).
- `infrastructure/pg/schema.py`: idempotent DDL for five tables plus four
  tenant indexes.
- `agent/memory/store.py`: tenant enforced, `'*'` permission rejected, SQL
  pushdown with `ORDER BY updated_at DESC LIMIT`, warn on outage.
- `agent/context/memory_providers.py`: outage raises `MemoryOutage` and the
  assembler transparently skips the layer.

### Quality

- 70 → 78 tests, passing offline via the `PG_OFFLINE` fallback. CI green on
  3.12 and 3.13. No hardcoded secrets.

### Known follow-ups

- `AgentTask` field wiring
- A real `EpisodicMemoryProvider`
- `@app.on_event("startup")` → lifespan handler
- Replacing the in-memory Tracer with OTel / Prometheus
- Carried over: real-PG kill-process restore (database name TBD)

## [0.2.0] — 2026-09-16

Durable checkpointing.

### Added

- Checkpointer that prefers Postgres and falls back to memory, honouring
  `PG_DSN`.
- Durable expense graph: `thread_id` pause and resume, `interrupt`, and a
  dual-write to `ApprovalStore`.
- Node-level guards: 5 s timeout, 3 retries, 1 s / 2 s / 4 s backoff,
  idempotency keys, loop limit of 8, and fault-chain tracing.

### Quality

- 34 → 43 tests; CI green on 3.12 and 3.13.

### Known limitations

Stated honestly in the release: the real-PG and LangGraph paths were verified
by mock shape only. Kill-process restore against a real database is still
pending database-name confirmation. `langgraph` and `redis` remain optional
dependencies so the offline path stays green.

## [0.1.0] — 2026-09-15

First usable milestone: the hand-built Agent Runtime end to end, TDD, 34
tests.

### Added

- Stage 0 — scaffolding and the FastAPI gateway
- Stages 1/1b — `AgentState`, the Planner/Executor loop, DeepSeek planner
- Stage 2 — Tool Registry plus the four ops tools (metrics, logs, git, db)
- Stage 3 — enterprise RAG with hard tenant and permission filtering
- Stage 4 — five-layer memory (Conversation, Task, User, Org, Episodic)
- Stage 5 — `PolicyEngine` and `ApprovalStore`
- Stage 6 — deterministic workflow (auto-approve or route to a human)
- Stage 7 — `Tracer` and `Evaluator` (TaskSuccess, PolicyViolation)
- Stage 8 — LangGraph alignment layer and the three scenarios

### Changed

The gateway port moved from `:8761` to `:8762` in v0.2.0 so it does not
collide with the scm-platform gateway, which also uses 8761. `.env.example`
now carries `AGENT_PORT=8762` and `SCM_GATEWAY_URL=http://localhost:8761`.

[Unreleased]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dengxingzhi000/cognora-enterprise-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dengxingzhi000/cognora-enterprise-agent/releases/tag/v0.1.0
