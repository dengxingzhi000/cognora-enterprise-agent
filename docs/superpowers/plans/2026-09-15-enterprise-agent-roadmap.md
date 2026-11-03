# Enterprise Agent Implementation Plan

> **Status as of 2026-11-03.** This roadmap started on 2026-09-15 and was
> advanced stage by stage. The checkboxes below now reflect **actual code**
> (bare install: 183 passed / 8 skipped; with the optional observability +
> langgraph + mcp extras: 187 / 6; with a real `AGENT_TEST_PG_DSN`: 192 / 1).
> Optional capabilities stay optional — see each item for the condition that
> activates them.

**Goal:** 带练方式交付企业级 AI Operations Agent，覆盖 Runtime/Tool/RAG/Memory/Policy/HITL/Workflow/Eval/Obs。

**Architecture:** FastAPI Gateway → 自研 Agent Runtime (Planner/Executor loop) → Tool Registry → MCP/DB/RAG → Policy/HITL → Workflow → Observability。先自研，后 LangGraph 重构。

**Tech Stack:** Python 3.13+, FastAPI, Pydantic, DeepSeek (OpenAI兼容), Postgres/pgvector + Redis (远程 `192.168.0.121`, DB `db_agent` for agent tables), Docker(后补), OpenTelemetry/Prometheus(可选)。

**Workspace path:** `D:\ProgramProject\scm-platform\enterprise-agent` (nested under the scm-platform workspace, separate git repo).

---

### Task 0: 脚手架 + Gateway (已完成)

**Files:**
- Create: `pyproject.toml`, `apps/api/main.py`, `tests/test_health.py`, `.env.example`
- Test: `tests/test_health.py`

- [x] **Step 1: 安装依赖**
Run: `pip install -e .` in `D:\ProgramProject\scm-platform\enterprise-agent`
Expected: 安装成功
- [x] **Step 2: 跑测试**
Run: `pytest -q`
Expected: 全绿（裸装 183 passed / 8 skipped；DSN 门控与可选依赖测试会 skip）
- [x] **Step 3: 启动网关**
Run: `uvicorn apps.api.main:app --port 8762`
Expected: GET /health 返回 `{"status":"ok"}`，GET /metrics 有 prometheus-client 时 200、否则 503

### Task 1: Agent Runtime (已完成)
- [x] Create: `agent/runtime/state.py` (AgentState: task/messages/plan/observations/tool_calls/iteration/status)
- [x] Create: `agent/runtime/loop.py` (Planner→Executor→Observation→Reflection循环, max_iterations=8)
- [x] Create: `agent/runtime/planner.py`, `agent/runtime/executor.py` (先Mock LLM + Mock Tool)
- [x] Test: `tests/test_runtime_loop.py`
- [x] AgentTask 状态机 + token/cost 记录 + HITL paused 迁移

### Task 2: Tool/MCP (已完成: MCP stub 为可选依赖)
- [x] Create: `agent/tools/registry.py`, `agent/tools/builtin.py`
- [x] MCP Server stub for 运维三件套: metrics/logs/git (`agent/mcp/server.py`)
- [ ] 真实 MCP 进程常驻部署（需 `pip install -e .[mcp]`；当前仅 stub + fake 传输测试）

### Task 3: Knowledge RAG (已完成: 离线 embedding + 可选 pgvector)
- [x] ingestion/chunking/metadata(tenant_id/department/permission/version)
- [x] `knowledge/embedding.py` — 本地确定性 feature-hashing embedding（无需模型/网络）
- [x] `knowledge/vector_index.py` — 内存向量索引 + 加权 rerank
- [x] `knowledge/store.py` — 先做租户/权限硬过滤，再做向量召回与 rerank
- [ ] pgvector 后端真实落库：需 `RAG_PGVECTOR_ENABLED=1`、DSN、且数据库安装 `vector` 扩展（虚拟机上当前未安装，集成测试会 skip）

### Task 4: Memory (Conversation/Task/User/Org/Episodic) — 已完成
- [x] `agent/memory/store.py` 五层抽象 + connector 注入
- [x] `agent/memory/episodic.py`：AgentTask 状态迁移写入 `memory_episodic`，按任务类型/工具/2-gram 相似度跨任务检索
- [x] `EpisodicMemoryProvider` 接入默认 provider 列表
- [x] Episodic 的 PG 持久化：`AGENT_PG_DSN` 指向 `db_agent`（已实测跨进程检索命中）；无 DSN 时为进程内列表

### Task 5: Policy + HITL (已完成: 审批单/挂起任务可持久化)
- [x] `security/policy.py` Role/Resource/Action，高风险需审批
- [x] `security/approval.py` repository 抽象：InMemory 默认 / Postgres 可选
- [x] `agent/runtime/paused_tasks.py` 挂起任务序列化与恢复
- [ ] 多进程共享需要 `AGENT_PG_DSN` 指向 `db_agent`，并执行 `scripts/db/agent_schema.sql`

### Task 6: Workflow (已完成)
- [x] `workflow/graph.py` 确定性流程（报销）
- [x] `workflow/durable.py` timeout/retry/idempotency/loop guard
- [x] `workflow/graph_lang.py` 真 `StateGraph` 路径；`langgraph` 可选，缺席走仿真路径
- [x] 真 PG checkpoint：`langgraph-checkpoint-postgres` + `AGENT_TEST_PG_DSN` 集成测试

### Task 7: Eval + Observability (已完成: OTel/Prometheus 为可选)
- [x] `evaluation/evaluator.py` TaskSuccess/ToolAcc/RAGRecall/PolicyViolation/Latency/Cost
- [x] `observability/tracing.py` 内存 Tracer（始终保留）
- [x] `observability/otel.py` OTel 适配层（需 `pip install -e .[observability]`；未安装返回 None）
- [x] `GET /metrics` Prometheus 指标；未安装 `prometheus-client` 时 503 并说明
- [ ] OTLP exporter 常驻采集需要额外安装 `opentelemetry-exporter-otlp`（当前只接 SDK 适配层）

### Task 8: LangGraph重构 + 三场景扩展 (已完成: 场景由 fixture 驱动)
- [x] LangGraph 真图路径 + 内存 saver 离线测试 + PG checkpoint 集成测试
- [x] IT 运维场景 `run_it_ops`
- [x] 合同合规 `review_contract`：输入文件/dict → 条款+金额规则 → 知识库 → supplier 工具 → 结构化结论
- [x] 销售分析 `analyze_sales`：`workflow/data/sales_week.json` 聚合 → metrics/knowledge → 结构化结论
- [ ] 真实外部系统接入：SCM 工具需 `SCM_GATEWAY_URL`；当前场景在未配置时退化为 mock 工具或本地样例

### Test baseline (verified 2026-11-03)

```bash
pip install -e . && pytest -q
# 183 passed, 8 skipped (DSN/pgvector integration tests + optional-dependency probes)

pip install -e ".[observability,langgraph,mcp]" && pytest -q
# 187 passed, 6 skipped (only the DSN/pgvector integration tests)

AGENT_TEST_PG_DSN=postgresql://<user>:<password>@192.168.0.121:5432/db_agent pytest -q
# 192 passed, 1 skipped (pgvector extension unavailable on that Postgres image)
```
