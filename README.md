<div align="center">

**English** · [简体中文](./README.zh-CN.md)

# Cognora Enterprise Agent

### A self-built AI operations agent with durable HITL, tools/MCP, RAG, and observable workflows.

**One FastAPI gateway. A hand-written Planner/Executor loop. A fully offline path with no cloud account, no paid dependency, and no credentials.**

[![Build](https://img.shields.io/github/actions/workflow/status/dengxingzhi000/cognora-enterprise-agent/ci.yml?branch=master&style=flat-square&logo=github&label=build)](https://github.com/dengxingzhi000/cognora-enterprise-agent/actions)
[![Release](https://img.shields.io/github/v/release/dengxingzhi000/cognora-enterprise-agent?style=flat-square&label=v0.5.9)](https://github.com/dengxingzhi000/cognora-enterprise-agent/releases/tag/v0.5.9)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square)](./LICENSE)

**[Quick start](#quick-start) · [Highlights](#highlights) · [Tech stack](#tech-stack) · [Architecture](#architecture) · [What it covers](#what-it-covers) · [Packages](#packages) · [Operations](#operations) · [FAQ](#faq) · [Docs](#documentation)**

<code>pip install -e . && uvicorn apps.api.main:app --port 8762</code>

</div>

> [!IMPORTANT]
> **The offline path is the default.** With no `AGENT_PG_DSN`, approval and paused-task state live in memory, episodic memory falls back to an in-process list, and every `SCM_*` tool stays mocked. Postgres persistence, MCP, Prometheus, OpenTelemetry, LangGraph, and pgvector are opt-in and never required by `pip install -e .`.

---

Building an enterprise agent is easy to demo and hard to operate. The demo path is a prompt plus a tool call; the part that matters is what happens when a tool times out, a write needs approval, the process restarts, the same incident returns, or the model has to choose between an exact policy search and a paraphrased question.

This repository is a working reference implementation. It keeps a hand-written Planner/Executor loop instead of hiding it behind a framework, exposes the same tools through an optional MCP server, persists HITL state and checkpoints in Postgres when a DSN is provided, adds a local embedding/vector/rerank path for retrieval, and traces every stage through an in-memory Tracer with optional Prometheus and OpenTelemetry adapters.

Everything runs locally. `pip install -e .`, start Uvicorn, and the gateway is on port 8762 with no cloud account and no paid dependency.

## Quick start

> [!NOTE]
> **No DSN, no key, no external service required.** `DEEPSEEK_API_KEY` empty means the planner falls back to `echo`; `SCM_GATEWAY_URL` unset means the SCM tools are mocks. That is what keeps the offline suite green and CI hermetic.

### Prerequisites

- **Python 3.12 or 3.13**
- **Postgres 14+** only if you want durable approvals, paused tasks, episodic memory, or LangGraph checkpoints
- **No Node.js, no Docker, no Nacos, no Maven** for this repository

### 1. Install

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -e .
```

Optional capability groups:

```bash
pip install -e ".[mcp]"                 # MCP server
pip install -e ".[observability]"       # prometheus-client + OpenTelemetry SDK/API
pip install -e ".[langgraph]"           # real StateGraph + Postgres checkpointer
pip install -e ".[mcp,observability,langgraph]"
```

### 2. Configure (optional)

```bash
copy .env.example .env    # Windows
cp .env.example .env     # Linux / macOS
```

A blank `DEEPSEEK_API_KEY` is valid and selects the deterministic `echo` planner. The agent never reads `AGENT_PG_DSN` from a checked-in file; set it in the shell or deployment environment.

### 3. Run

```bash
uvicorn apps.api.main:app --port 8762 --reload
```

Open <http://localhost:8762/docs> for the OpenAPI UI. `GET /health` returns `{"status":"ok","stage":1}`.

### 4. Verify

```bash
pytest -q
# 185 passed, 8 skipped on a bare install
```

See [Testing](#testing) for the optional-extras and real-Postgres numbers.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe |
| `GET` | `/metrics` | Prometheus text metrics when `prometheus-client` is installed; otherwise 503 with a reason |
| `GET` | `/docs` | OpenAPI UI |
| `POST` | `/chat` | Run one agent task (tenant header/body, user, task text) |
| `POST` | `/approval/{approval_id}/decide` | Approve or reject the approval a paused task is waiting on |
| `POST` | `/chat/{task_id}/resume` | Execute the single approved action, or cancel when rejected |

### Environment variables

Every value has a working offline default. Secrets belong in the environment, never in the repository:

| Variable | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | empty | DeepSeek key; empty selects the `echo` planner |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Model id, also the pricing key for cost accounting |
| `SCM_GATEWAY_URL` | unset | **Unset ⇒ every `scm.*` tool is a mock.** Set it to use real REST calls |
| `SCM_AUTH_URL` | `http://localhost:8106` | cognora-aura-platform auth service |
| `SCM_USERNAME` / `SCM_PASSWORD` | empty | cognora-aura-platform credentials |
| `SCM_TENANT_MAP` | `{"t1":"tenant_001"}` | Maps the agent's `tenant_id` onto SCM's `tenant` parameter |
| `SCM_TIMEOUT_SECONDS` | `5` | Per-request timeout |
| `AGENT_PORT` | `8762` | Gateway port |
| `AGENT_PG_DSN` | unset | **The only switch** for approvals, paused tasks, episodic memory, and checkpoints; point it at `db_agent` |
| `AGENT_PG_OFFLINE` | unset | Force agent persistence and the memory store offline regardless of DSN |
| `AGENT_APPROVAL_LEASE_SECONDS` | `300` | Base lease held by the resume request that atomically claimed an approval; it is extended while the tool runs, so it does not need to exceed the longest tool duration |
| `AGENT_APPROVAL_HEARTBEAT_ENABLED` | `true` | Execution-time lease heartbeat; `0`/`false` disables it (used by the drill that proves a heartbeat-less lease still expires) |
| `AGENT_APPROVAL_HEARTBEAT_INTERVAL_SECONDS` | lease / 3 | Renewal interval override; unset derives it from the lease, invalid values fall back to the derived value. Startup fails if `2 * interval >= lease` (see below) |
| `AGENT_APPROVAL_RECLAIM_ON_RESUME` | `true` | Lazily reclaim expired `in_progress` claims at the resume entry point |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `120` | Per-tool execution timeout. SCM write calls normally return in seconds; 120 s covers retries and slow queries without letting a stuck tool hold `in_progress` forever |
| `AGENT_TOOL_EFFECT_STALE_SECONDS` | tool timeout + lease (`420` by default) | Age after which a `started` write-tool effect may be reclaimed, but only if its approval no longer holds a live lease. Default derivation: `AGENT_TOOL_TIMEOUT_SECONDS` (120) + `AGENT_APPROVAL_LEASE_SECONDS` (300) = 420 s. Lowering it unlocks crashed effects sooner and raises the chance of mistaking a slow live tool for a stuck one |
| `AGENT_INSTANCE_ID` | `hostname:pid` | Claim owner written to `claimed_by`; used by claim/renew and by the lazy reclaim guard so an instance never reclaims its own live lease |
| `AGENT_TEST_PG_DSN` | unset | Opt-in trigger for real-Postgres integration tests; never read by the application |
| `RAG_EMBEDDING_PROVIDER` | `hashing` | Local deterministic embedding; no external model required |
| `RAG_PGVECTOR_ENABLED` | unset | `1` enables the optional pgvector backend (needs the `vector` extension and a DSN) |
| `RAG_PGVECTOR_DSN` | unset | pgvector DSN; falls back to `AGENT_PG_DSN` / `PG_DSN` / `POSTGRES_DSN` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Enables the real OTLP exporter when the observability group is installed; unset keeps the in-memory Tracer |
| `POSTGRES_DSN` / `PG_DSN` | unset | **Legacy** five-layer MemoryStore only; does not activate agent persistence |
| `CONTEXT_BUDGET_TOKENS` | `8000` | Total context-assembly budget |
| `CONTEXT_RESERVED_TOKENS` | `1000` | Headroom reserved for the reply |
| `CONVERSATION_KEEP_RECENT` | `3` | Recent conversation turns kept verbatim |
| `OBSERVATION_KEEP_RECENT` | `5` | Recent observations kept verbatim |
| `SCM_RETRY_BACKOFF_SECONDS` | `0.3` | Sleep before the single retry on 5xx / `ConnectError` / `Timeout` |

> [!WARNING]
> **Credentials are never checked in.** The examples in `.env.example`, README, and `scripts/db/agent_schema.sql` use `<user>` / `<password>` placeholders. A real DSN is supplied by the deployment environment. `AGENT_PG_DSN` is the only switch for agent persistence; `PG_DSN` / `POSTGRES_DSN` are legacy memory-store settings and do not turn it on.

## Highlights

| Icon | Feature | Description |
| --- | --- | --- |
| 🔁 | **Agent loop** | Hand-written Planner → Executor → Observation → Reflection loop, max 8 iterations, explicit `Finish` / `call_tool` contract |
| 🧰 | **Tool registry** | One dispatch path for built-in ops tools, SCM REST tools, and MCP |
| 🧩 | **MCP server** | `metrics.get`, `logs.search`, `git.diff` exposed with MCP-shaped results; `mcp` is optional |
| 🛡️ | **Policy** | Read ⇒ allow, write ⇒ approval, unknown ⇒ deny; cross-tenant writes are rejected |
| ✋ | **Durable HITL** | Claim-then-execute approvals (atomic claim + heartbeat-renewed lease + lazy reclaim) and paused `AgentState` survive a restart when `AGENT_PG_DSN` points at `db_agent` |
| 🧠 | **Memory** | Five layers plus real episodic recall (task type, tools, lexical similarity); tenant isolation before ranking |
| 📚 | **Retrieval** | Deterministic hashing embeddings, in-memory cosine index, weighted lexical/vector rerank; pgvector is opt-in |
| 🔀 | **Workflow** | Hand-rolled durable guards (timeout / retry / idempotency / loop limit) and an optional real LangGraph `StateGraph` |
| 📉 | **Observability** | In-memory Tracer, optional Prometheus `/metrics`, optional OTel span adapter |
| 🧪 | **Scenarios** | IT operations, contract compliance, and sales analysis over real dict/JSON fixtures; missing data returns `insufficient_data` |
| 🧾 | **Agent task** | State machine, token usage, per-model cost, status history ring buffer |
| 🔒 | **Security** | Explicit policy layer; no raw exception text on the wire; tenant-scoped repositories |

## What is live vs conditional

| Status | Capability | What unlocks it | Verification |
| --- | --- | --- | --- |
| **Always on** | Hand-written runtime, Tool Registry, Policy/HITL transitions, local hashing RAG, in-memory episodic memory, in-memory approvals/paused tasks, in-memory Tracer, fixture scenarios | `pip install -e .` only | CI (unit/fixture) |
| **Conditional** | Durable approvals / paused tasks / episodic / checkpoints | `AGENT_PG_DSN` pointing at `db_agent` (5432), schema already applied | CI: real-PG concurrency, lease heartbeat/reclaim and kill/restart drills |
| **Conditional** | pgvector backend | `RAG_PGVECTOR_ENABLED=1` + a Postgres with the `vector` extension. The verified test instance is `192.168.0.121:5434/db_agent_vector` (`extversion=0.8.7`); **do not use 5433**, which belongs to another project | Real instance verified manually (5434, vector 0.8.7); CI exercise uses the in-memory index only |
| **Conditional** | MCP server | `pip install -e ".[mcp]"`; `python -m agent.mcp.server` runs stdio only | Tool registration is unit-tested in CI; the stdio launch is manual and is not a deployed daemon |
| **Conditional** | Prometheus `/metrics` | `pip install -e ".[observability]"` | CI unit tests cover the 200 and 503 responses |
| **Conditional** | OTLP spans | `pip install -e ".[observability]"` + `OTEL_EXPORTER_OTLP_ENDPOINT` (gRPC 4317 / HTTP 4318); unset keeps the in-memory Tracer | Exporter wiring is unit-tested in CI; real collector delivery was verified manually (4317/4318, metrics on 8889); no vendor backend |
| **Conditional** | Real LangGraph graph / PG checkpoints | `pip install -e ".[langgraph]"`; PG checkpoints also need `AGENT_PG_DSN` | Graph tests run in CI with extras; PG checkpoints were verified manually and skip without a DSN |
| **Conditional** | Real SCM tools | `SCM_GATEWAY_URL` + auth settings; otherwise tools stay mocked | Mock path runs in CI; a real gateway is not verified by this repository |
| **Not claimed** | Production collector/alerting, OTLP backend retention, MCP HTTP daemon; VM real-environment drills, cross-host ownership, exactly-once side effects | The CI drills use a runner-local Postgres and bounded single-process restarts; `claimed_by` is instance attribution, not distributed locking, and `agent_tool_effect` only covers writes invoked through this framework | — |

**Verification labels:** **CI** means every green build exercises the path.
**Manual** means it was run once against the instance named in the row and is
not repeated by CI. A manual result is not a CI guarantee.

## Tech stack

| Layer | Components |
| --- | --- |
| **Runtime** | Python 3.12 / 3.13, FastAPI, Uvicorn, Pydantic |
| **Agent** | Hand-written planner/executor loop, `AgentTask` state machine, token/cost accounting |
| **LLM** | DeepSeek (OpenAI-compatible) with a deterministic `echo` fallback |
| **Tools** | In-process Tool Registry, SCM REST adapter, optional MCP 1.x/2.x server |
| **RAG** | Local feature-hashing embeddings, in-memory cosine index, lexical/vector rerank; optional pgvector |
| **Memory** | User / Org / Conv / Task / Episodic layers, `MemoryStore`, tenant/permission filters |
| **Persistence** | PostgreSQL + psycopg 3 + SQLAlchemy; InMemory repositories when no `AGENT_PG_DSN` |
| **Workflow** | Hand-rolled graph + durable guards; optional LangGraph `StateGraph` and `PostgresSaver` |
| **Observability** | In-memory Tracer, optional Prometheus, optional OpenTelemetry span adapter |
| **Testing** | pytest, `pytest -q`, 183 / 187 / 192 passing baselines depending on optional deps and DSN |
| **CI** | GitHub Actions, Python 3.12/3.13 matrix, `contents: read`, concurrency cancellation |

## Architecture

```
Client / curl / OpenAPI UI
        │
        ▼
FastAPI gateway — apps/api/main.py :8762
  /health · /metrics · /docs · /chat · /approval · /resume
        │
        ▼
Agent Runtime — planner → executor → observation → reflection
  AgentTask state machine · token/cost · status history
        │
        ├── Tools   → built-in ops / SCM REST (mock default) / MCP (optional)
        ├── Context → budget/compress → memory providers → episodic recall
        ├── RAG     → hashing embedding → vector index → rerank → pgvector?
        ├── Policy  → allow / need_approval / deny → approval + paused stores
        └── Workflow→ durable guards → LangGraph StateGraph? → checkpoint
        │
        ▼
Observability — in-memory Tracer → Prometheus? / OpenTelemetry?
```

There are no cross-process RPC calls, no service registry, and no message broker in this repository. The optional external dependencies are Postgres, SCM, MCP, Prometheus, OpenTelemetry, and LangGraph; all are activated by explicit environment variables or optional packages.

## What it covers

Every scenario below is implemented by the listed code and tested against fixtures or the offline mock path. The right-hand column names the real constraint, not a marketing claim.

| Scenario | What the agent does | Constraint |
| --- | --- | --- |
| **IT operations** | Accepts an incident question, calls metrics/logs/git, consults the knowledge base, and returns a grounded diagnosis | `run_it_ops`; SCM/ops tools mock unless `SCM_GATEWAY_URL` is set |
| **Contract compliance** | Loads a contract dict/JSON, validates required fields, flags risky clauses and amount thresholds, retrieves policy, and calls the supplier tool when an id is present | `review_contract`; missing data returns `insufficient_data`, never invented results |
| **Sales analysis** | Reads `workflow/data/sales_week.json`, computes GMV / orders / AOV / failed payments / category changes, then adds metrics and knowledge calls | `analyze_sales`; empty or missing data returns `insufficient_data` |

> [!NOTE]
> The three scenarios are demonstration flows, not connectors to a production BI or contract system. Real SCM data is used only when `SCM_GATEWAY_URL` is configured; otherwise the mock path is the documented default.

## Packages

| Package | Responsibility |
| --- | --- |
| `apps/api` | FastAPI gateway, `/chat`, `/approval`, `/resume`, `/metrics`, startup wiring |
| `agent/runtime` | Planner/Executor loop, `AgentState`, `AgentTask`, paused-task persistence |
| `agent/tools` | Tool Registry, guarded executor, built-in ops tools |
| `agent/mcp` | Optional MCP server and pure spec/call helpers |
| `agent/context` | Budgeted context assembler, compression, memory providers |
| `agent/memory` | Five-layer `MemoryStore` and `EpisodicMemoryStore` |
| `knowledge` | Documents, ingestion, hashing embeddings, vector index, rerank, pgvector option |
| `security` | `PolicyEngine`, `ApprovalStore`, InMemory/Postgres repositories |
| `workflow` | Hand-rolled graph, durable guards, checkpoint adapters, scenarios, fixtures |
| `observability` | In-memory Tracer, Prometheus metrics, OTel adapter |
| `infrastructure/pg` | Connector abstraction, Postgres/InMemory implementations, DDL |
| `evaluation` | Case-based evaluator |
| `scripts/db` | `agent_schema.sql` for `db_agent` |
| `tests` | Offline suite plus opt-in Postgres/pgvector integration tests |

### Project structure

```
cognora-enterprise-agent/
├── apps/api/                # FastAPI gateway
├── agent/
│   ├── runtime/             # loop · state · agent_task · paused_tasks
│   ├── tools/               # registry · executor · builtin tools
│   ├── mcp/                 # optional MCP server
│   ├── context/             # assembler, budget, compression, memory providers
│   └── memory/              # store + episodic
├── knowledge/               # embedding · vector_index · store · pgvector
├── security/                # policy · approvals
├── workflow/                # graph · checkpoint · scenarios · data/
├── observability/           # tracing · metrics · otel
├── infrastructure/pg/       # connector · schema
├── evaluation/              # evaluator
├── scripts/db/              # agent_schema.sql
├── tests/                   # offline + opt-in integration tests
└── docs/superpowers/        # design specs and implementation plans
```

## Key architecture patterns

### Durable HITL and exact-match approvals

Write tools cannot execute just because a user asked. `PolicyEngine` returns `allow` for reads, `need_approval` for writes, and `deny` for unknown or cross-tenant actions. The privileged executor only proceeds when an approval is approved, unconsumed, and its stored `tool` + `args` exactly match the pending action. `POST /approval/{id}/decide` records the decision; `POST /chat/{task_id}/resume` executes the one approved action or cancels the task.

With `AGENT_PG_DSN`, approvals live in `agent_approval` and the whole `AgentState` snapshot (including `AgentTask`) lives in `agent_paused_task`, keyed by `(tenant_id, task_id)`. Without a DSN they stay in process memory; a restart loses pending approvals, but the offline suite never needs a database.

Resume is claim-first: an atomic conditional `UPDATE ... WHERE status='approved' AND consumed=FALSE` moves the row to `in_progress` with a lease **and a fresh `claim_token` UUID**, and only the winner executes. Renewals, `complete` and `fail` all carry `AND claim_token = $token`: a stale claimant that lost its lease affects **0 rows**, writes no terminal state at all, and its response is **409 `approval lease lost`** — it can never overwrite the new claimant's row. While the tool runs, a background heartbeat renews `lease_until` every `lease/3`, so **the lease no longer has to exceed the longest tool runtime**. The heartbeat is conditional too (`status='in_progress' AND lease_until > now()`), so a row that was already reclaimed is never renewed; a 0-row renew (or repeated database errors past the failure threshold) stops the loop and the response becomes **409 lease lost** instead of a misleading `done`. Startup refuses an invalid margin: `max_failures * interval` must be `< lease` (fail-fast, no silent downscaling). A tool that outlives `AGENT_TOOL_TIMEOUT_SECONDS` is cancelled, the approval becomes `failed` with the fixed text `tool timeout`, and the heartbeat stops — a stuck tool cannot stay `in_progress` forever just because the heartbeat keeps renewing. If the process is SIGKILLed the heartbeat dies with it, the lease lapses, and the next resume reclaims the row; the effect row (below) then decides whether to replay a stored result or allow one retry.

Write-tool idempotency lives in `agent_tool_effect`, keyed by `(tenant_id, approval_id, tool_name, args_hash)`. Before the tool runs, resume reserves the row with `INSERT ... ON CONFLICT DO NOTHING` (`status=started`); only the reservation winner calls the tool. A later attempt with the same approval, tool and args sees `succeeded` and replays the stored reference (`sha256:` digest, never the raw payload) **without executing**, sees `started` and gets **409 `tool execution already started`** (no parallel run), or sees `failed` and gets one retry (`attempt=2`; the third attempt is refused). `args_hash` is a stable serialization: keys sorted recursively, volatile fields (`tenant_id`, request/trace IDs, timestamps) removed, compact JSON, sha256. A timeout records `failed` plus `maybe_side_effect=true`, so callers can tell an ambiguous failure from a clean one.

A crash between reserving `started` and writing the result would otherwise pin the effect at `started` and reject every retry with 409. Lazy reclaim therefore also expires such rows: a `started` effect older than `AGENT_TOOL_EFFECT_STALE_SECONDS` becomes `failed` with `maybe_side_effect=TRUE` and the fixed `last_error` `stale started effect reclaimed`, which puts it back into the one-retry budget (`attempt < 2`). The reclaim predicate requires **all** of: `status='started'`, `updated_at` older than the stale window, and its approval no longer active (`status <> 'in_progress'` or `lease_until <= now()`, or the approval row is gone). **As long as the approval still holds a live lease, the effect is never reclaimed.** This runs at startup and on every lazy `reclaim_expired_claims()` in resume — no extra thread — and repeated runs are idempotent (the conditional update hits 0 rows). `tool_effect_reclaimed_total` counts reclaimed rows.

For operators, `scripts/ops/stuck_tool_effects.py` is a read-only view of stuck effects (tenant, approval, tool, first 8 chars of `args_hash`, status, attempt, `maybe_side_effect`, `updated_at`, and the approval's status/lease). It is a maintenance script rather than an admin endpoint: no new network/auth surface, usable while the app cannot start, and it never prints plaintext args or results.

Known limitations:
- Replay bound for the covered case: for the **same approval + same tool + same args**, when the result was recorded (`succeeded`) a crashed/retried resume executes the write **zero** times. If the crash happened before the result was written, stale reclaim marks the effect `failed(maybe_side_effect=true)` and permits one retry — the side effect may already exist. This is not "zero replay in any scenario": different args are a different effect, tools invoked outside this framework are unprotected, and a tool still running in an abandoned worker thread after a timeout is not stopped.
- Multi-instance: `claimed_by` (from `AGENT_INSTANCE_ID`, default `hostname:pid`) is written by claim and checked by renew; lazy reclaim skips an instance's **own live** lease. Another instance may still reclaim a lease that has genuinely expired — that is intended crash recovery, not a bug. `claimed_by` does **not** provide cross-host fencing or exactly-once side effects: two processes sharing the same `AGENT_INSTANCE_ID`, a paused host, or a tool that already applied a side effect remain unprotected. A multi-instance deployment should use distinct instance IDs and treat this as at-most-once *attempts*, not distributed locking.
- Wall-clock tests: the heartbeat/reclaim tests (`tests/test_approval_heartbeat.py`, `test_approval_concurrency.py`) depend on real time (sleeps, lease fast-forwarding). They are regression guards, **not a logical invariant** — a loaded CI runner can make them flaky.
- `execute_with_timeout` runs the tool on a worker thread and cannot kill it: after a timeout the abandoned thread may still finish in the background. The effect row records `maybe_side_effect=true`, but that flag is information, not a rollback.
- Stale reclaim is a judgement call, not proof: a tool that is genuinely slow but still running while its approval lease has lapsed is indistinguishable from a crashed one. A lower `AGENT_TOOL_EFFECT_STALE_SECONDS` unlocks sooner and misjudges sooner; the default is timeout + one lease for that reason.

### Episodic memory

Every `AgentTask` status transition is appended to `memory_episodic`. Recall scores by task type, tool names, and 2-gram/keyword overlap, with tenant filtering applied before ranking. It deliberately does not need an external vector database; the lexical path is deterministic and testable offline.

### Retrieval with hard tenant/permission filters

`KnowledgeStore` filters candidates by tenant and permission **before** embedding and vector scoring. The default embedding provider is local feature hashing, so `pip install -e .` has no model download and no network dependency. `pgvector` is a separate opt-in backend behind `RAG_PGVECTOR_ENABLED=1`; if the extension or DSN is missing it falls back to the in-memory store.

A real pgvector instance was verified separately: `192.168.0.121:5434/db_agent_vector`, extension `vector 0.8.7`. `tests/test_rag.py` passes **7/7** against it and the vector-hit test first returns the ops document. Do not reuse port **5433**, which is occupied by another project. The main persistence DB remains `192.168.0.121:5432/db_agent`; it has no `vector` extension, so the pgvector test there is skipped as designed.

### Real LangGraph when installed

`workflow/graph_lang.py` runs a genuine `StateGraph` when `langgraph` is installed and the checkpointer is native; otherwise it runs an honest simulation path. Pause/resume uses `Command(resume=...)` on LangGraph 1.x, and `PostgresSaver` checkpoints are persisted in `db_agent`. The hand-rolled `workflow/graph.py` remains the no-dependency path.

### MCP is stdio-only, by design

`python -m agent.mcp.server` starts the MCP server on stdio. It is deliberately **not** a long-running HTTP service: MCP clients spawn the process as a child and communicate over stdin/stdout, so there is no port, no daemon, and no background service to deploy. A long-lived HTTP transport would be a separate design (session handling, auth, lifecycle) and is not implemented here; the current release does not claim a deployed MCP daemon.

### Observability without vendor lock-in

The in-memory `Tracer` always records the task lifecycle. If `prometheus-client` is installed, `GET /metrics` renders counters for task status, tool calls, and trace events. If `opentelemetry-*` plus `opentelemetry-exporter-otlp` are installed **and** `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the same events are mirrored into spans through a real `SimpleSpanProcessor` / OTLP exporter (gRPC 4317 or HTTP 4318; `_protocol_for` picks by port). Otherwise the code logs which path it took and keeps the in-memory Tracer; missing packages or endpoints never fail startup. The endpoint is not a secret, but no DSN/password is ever logged.

The verification collector accepted OTLP metrics; `curl http://192.168.0.121:8889/metrics` exposed `agent_verify_points_total{result="ok"} 3` (HTTP) and `agent_verify_grpc_points_total{result="ok"} 7` (gRPC). The collector is a local HTTP/gRPC receiver with a Prometheus exporter and debug output only — no external backend, retention, or alerting, and it is not claimed as a production deployment.

## Operations

### Persistence

`AGENT_PG_DSN` is the only switch for approvals, paused tasks, episodic memory, and checkpoints. Create the dedicated database and apply the idempotent schema:

```bash
psql "postgresql://<user>:<password>@192.168.0.121:5432/postgres" \
     -c "CREATE DATABASE db_agent OWNER <user>;"

export AGENT_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5432/db_agent"
psql "$AGENT_PG_DSN" -f scripts/db/agent_schema.sql
```

If `psql` is unavailable, the same DDL can be executed with any Postgres client (for example `psycopg`). The script creates `agent_approval`, `agent_paused_task`, and all `memory_*` tables. LangGraph's `PostgresSaver.setup()` creates `checkpoints` / `checkpoint_*` on first use.

### CI and release

`.github/workflows/ci.yml` runs a Python **3.12 / 3.13** matrix on `master`, installs with `pip install -e .`, and runs `pytest -q -rs`. It cancels superseded runs and uses `permissions: contents: read`. CI intentionally has no `AGENT_TEST_PG_DSN`, `DEEPSEEK_API_KEY`, or `SCM_GATEWAY_URL`, so the Postgres/pgvector integration tests and optional-dependency probes skip by design; the offline suite must stay green.

`master` is protected: pull request required, force pushes and branch deletions disabled, no required review count. `v0.5.9` is an annotated tag; releases are created from tags.

### Testing

```bash
# Bare install (no optional extras, no DSN)
pip install -e . && pytest -q
# 185 passed, 8 skipped

# Optional extras, still no DSN
pip install -e ".[mcp,observability,langgraph]" && pytest -q
# 189 passed, 6 skipped

# Real Postgres integration (db_agent; no vector extension)
export AGENT_TEST_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5432/db_agent"
pytest -q
# 192 passed, 1 skipped (pgvector; measured before the boundary tests below)

# Real pgvector integration (db_agent_vector; vector 0.8.7)
export AGENT_TEST_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5434/db_agent_vector"
pytest -q
# 193 passed, 0 skipped (measured before the boundary tests below)
```

The `db_agent` skip is pgvector: that Postgres image has no `vector` extension. On `db_agent_vector` the full suite is green and `tests/test_rag.py` is **7 passed, 0 skipped**. Both databases were cleaned after verification (`agent_approval`, `agent_paused_task`, `memory_*`, `agent_knowledge_chunk` = 0 rows).

## FAQ

<details>
<summary><strong>Do I need Postgres or a model key to run the agent?</strong></summary>

No. With no `AGENT_PG_DSN`, approvals and paused tasks are in process memory and episodic memory is an in-process list. With no `DEEPSEEK_API_KEY`, the planner falls back to `echo` and every tool stays mocked. `pip install -e . && pytest -q` is the intended offline path.

</details>

<details>
<summary><strong>Why does <code>GET /metrics</code> return 503?</strong></summary>

`prometheus-client` is an optional dependency. Install `pip install -e ".[observability]"` to render Prometheus text. Without it, the endpoint returns 503 with an explanation instead of failing startup.

</details>

<details>
<summary><strong>Why is my graph running the simulation path?</strong></summary>

The real `StateGraph` requires the optional `langgraph` group **and** a native checkpointer. Without either, the code deliberately runs the hand-rolled simulation path. Install `pip install -e ".[langgraph]"` and pass `get_checkpointer(...)` to exercise the real graph.

</details>

<details>
<summary><strong>Why do the Postgres tests skip?</strong></summary>

They are gated on `AGENT_TEST_PG_DSN`. Set it to a reachable `db_agent` and run `pytest -q`; the pgvector test additionally requires the `vector` extension. The application never reads `AGENT_TEST_PG_DSN`.

</details>

<details>
<summary><strong>Why do all SCM tools return mock data?</strong></summary>

`SCM_GATEWAY_URL` is unset. That is the documented default, not a failure. Set the gateway URL (plus auth settings) to switch the tools to real REST calls.

</details>

## Documentation

- [AGENTS.md](./AGENTS.md) — repository working notes, architecture invariants, build/test commands
- [CHANGELOG.md](./CHANGELOG.md) — release history and activation conditions
- [CONTRIBUTING.md](./CONTRIBUTING.md) — offline-test rule, optional dependency policy, PR flow
- [docs/superpowers/plans/2026-09-15-enterprise-agent-roadmap.md](./docs/superpowers/plans/2026-09-15-enterprise-agent-roadmap.md) — stage roadmap
- [docs/superpowers/plans/2026-09-16-v0.5.0-roadmap.md](./docs/superpowers/plans/2026-09-16-v0.5.0-roadmap.md) — next candidates
- [SECURITY.md](./SECURITY.md) — vulnerability reporting
- [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — community expectations

Report vulnerabilities through [SECURITY.md](./SECURITY.md).

## Related projects

- **[cognora-aura-platform](https://github.com/dengxingzhi000/cognora-aura-platform)** — the Java/Spring Cloud supply-chain platform whose tools and auth this agent can call when `SCM_GATEWAY_URL` is configured.
- **[enterprise-agent](https://github.com/dengxingzhi000/enterprise-agent)** — the original repository for this agent's early history.

## Contributing

We'd love your help! [Open an issue](https://github.com/dengxingzhi000/cognora-enterprise-agent/issues)
to report a bug or suggest a feature, or send a pull request.

Please read [CONTRIBUTING.md](./CONTRIBUTING.md) first. It covers the offline-first test rule, the optional-dependency policy, and what CI checks.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a pull request

All notable changes are tracked in [CHANGELOG.md](./CHANGELOG.md). Everyone is expected to follow the [code of conduct](./CODE_OF_CONDUCT.md).

## License

[Apache License 2.0](./LICENSE)

---

<div align="center">

**Built for enterprise AI operations, with an offline path that actually runs.**

</div>
