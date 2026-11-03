<div align="center">

[English](./README.md) · **简体中文**

# Cognora Enterprise Agent

### 自研 AI 运维 Agent：持久化 HITL、Tools/MCP、RAG 与可观测工作流。

**一个 FastAPI 网关。手写 Planner/Executor 循环。无需云账号、无需付费依赖、无需任何凭据即可跑通离线路径。**

[![Build](https://img.shields.io/github/actions/workflow/status/dengxingzhi000/cognora-enterprise-agent/ci.yml?branch=master&style=flat-square&logo=github&label=build)](https://github.com/dengxingzhi000/cognora-enterprise-agent/actions)
[![Release](https://img.shields.io/github/v/release/dengxingzhi000/cognora-enterprise-agent?style=flat-square&label=v0.5.1)](https://github.com/dengxingzhi000/cognora-enterprise-agent/releases/tag/v0.5.1)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square)](./LICENSE)

**[快速开始](#快速开始) · [能力亮点](#能力亮点) · [技术栈](#技术栈) · [架构](#架构) · [覆盖场景](#覆盖场景) · [包结构](#包结构) · [运维](#运维) · [FAQ](#faq) · [文档](#文档)**

<code>pip install -e . && uvicorn apps.api.main:app --port 8762</code>

</div>

> [!IMPORTANT]
> **离线路径才是默认路径。** 没有 `AGENT_PG_DSN` 时，审批与挂起任务在内存中，Episodic 记忆退回进程内列表，所有 `SCM_*` 工具保持 mock。Postgres 持久化、MCP、Prometheus、OpenTelemetry、LangGraph、pgvector 都是可选能力，`pip install -e .` 永远不需要它们。

---

企业级 Agent 做 demo 很容易，做可运维很难。Demo 是一条 prompt 加一次工具调用；真正难的是工具超时、写操作需要审批、进程重启、同类故障再次出现，以及模型要在精确制度检索和改述问题之间做选择时，系统还能不能给出可解释的结果。

本仓库是一个可运行的参考实现：保留手写 Planner/Executor 循环而不是把控制流藏进框架；同一批工具可以额外通过可选 MCP server 暴露；提供 DSN 时审批、挂起任务与 checkpoint 落 Postgres；检索走本地 embedding + 向量索引 + rerank；每个阶段都进内存 Tracer，并可选接入 Prometheus 与 OpenTelemetry。

所有东西都能本地跑。`pip install -e .`，启动 Uvicorn，网关就在 8762；不需要云账号，也没有付费依赖。

## 快速开始

> [!NOTE]
> **不配 DSN、不配 key、不启外部服务也能跑。** `DEEPSEEK_API_KEY` 为空时 planner 退回 `echo`；`SCM_GATEWAY_URL` 未设置时 SCM 工具是 mock。这正是离线测试全绿、CI 保持封闭的原因。

### 前置条件

- **Python 3.12 / 3.13**
- **Postgres 14+**（仅在需要持久化审批、挂起任务、Episodic 或 LangGraph checkpoint 时）
- **不需要 Node.js、Docker、Nacos、Maven**

### 1. 安装

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -e .
```

可选能力组：

```bash
pip install -e ".[mcp]"                 # MCP server
pip install -e ".[observability]"       # prometheus-client + OpenTelemetry SDK/API
pip install -e ".[langgraph]"           # 真 StateGraph + Postgres checkpointer
pip install -e ".[mcp,observability,langgraph]"
```

### 2. 配置（可选）

```bash
copy .env.example .env    # Windows
cp .env.example .env      # Linux / macOS
```

`DEEPSEEK_API_KEY` 留空是合法配置，会使用确定性 `echo` planner。`AGENT_PG_DSN` 不要写进仓库文件，只在 shell 或部署环境中注入。

### 3. 启动

```bash
uvicorn apps.api.main:app --port 8762 --reload
```

打开 <http://localhost:8762/docs> 查看 OpenAPI。`GET /health` 返回 `{"status":"ok","stage":1}`。

### 4. 验证

```bash
pytest -q
# 裸装 183 passed, 8 skipped
```

可选依赖与真实 Postgres 的精确数字见[测试](#测试)。

### 接口

| Method | Path | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 存活探针 |
| `GET` | `/metrics` | 安装 `prometheus-client` 时输出 Prometheus 文本；否则 503 并说明原因 |
| `GET` | `/docs` | OpenAPI UI |
| `POST` | `/chat` | 运行一个 Agent 任务（tenant、user、task） |
| `POST` | `/approval/{approval_id}/decide` | 审批通过/驳回挂起任务等待的审批单 |
| `POST` | `/chat/{task_id}/resume` | 执行已批准的那一个动作；被驳回则取消任务 |

### 环境变量

所有值都有可运行的离线默认值。密钥只放在环境里，绝不提交：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek key；为空时使用 `echo` planner |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型 id，也是成本计价的 key |
| `SCM_GATEWAY_URL` | 未设置 | **未设置 ⇒ 所有 `scm.*` 工具走 mock。** 设置后才走真实 REST |
| `SCM_AUTH_URL` | `http://localhost:8106` | scm-platform auth 服务 |
| `SCM_USERNAME` / `SCM_PASSWORD` | 空 | scm-platform 凭据 |
| `SCM_TENANT_MAP` | `{"t1":"tenant_001"}` | 把 Agent 的 `tenant_id` 映射到 SCM 的 `tenant` 参数 |
| `SCM_TIMEOUT_SECONDS` | `5` | 单请求超时 |
| `AGENT_PORT` | `8762` | 网关端口 |
| `AGENT_PG_DSN` | 未设置 | **唯一开关**：审批、挂起任务、Episodic 记忆、checkpoint；指向 `db_agent` |
| `AGENT_PG_OFFLINE` | 未设置 | 无论 DSN 如何都强制 Agent 持久化与 MemoryStore 离线 |
| `AGENT_TEST_PG_DSN` | 未设置 | 真实 Postgres 集成测试的 opt-in 开关；应用本身不读取 |
| `RAG_EMBEDDING_PROVIDER` | `hashing` | 本地确定性 embedding，无外部模型 |
| `RAG_PGVECTOR_ENABLED` | 未设置 | `1` 开启 pgvector（需要 `vector` 扩展与 DSN） |
| `RAG_PGVECTOR_DSN` | 未设置 | pgvector DSN；回退到 `AGENT_PG_DSN` / `PG_DSN` / `POSTGRES_DSN` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 未设置 | 安装 observability 组后启用真实 OTLP exporter；未设置时保持内存 Tracer |
| `POSTGRES_DSN` / `PG_DSN` | 未设置 | **仅旧版**五层 MemoryStore；不会开启 Agent 持久化 |
| `CONTEXT_BUDGET_TOKENS` | `8000` | 上下文总预算 |
| `CONTEXT_RESERVED_TOKENS` | `1000` | 为回复保留的空间 |
| `CONVERSATION_KEEP_RECENT` | `3` | 保留的原样对话轮数 |
| `OBSERVATION_KEEP_RECENT` | `5` | 保留的原样观察条数 |
| `SCM_RETRY_BACKOFF_SECONDS` | `0.3` | 5xx / `ConnectError` / `Timeout` 单次重试前等待 |

> [!WARNING]
> **凭据绝不入库。** `.env.example`、README 与 `scripts/db/agent_schema.sql` 里的示例只使用 `<user>` / `<password>` 占位符。真实 DSN 由部署环境注入。`AGENT_PG_DSN` 是 Agent 持久化的唯一开关；`PG_DSN` / `POSTGRES_DSN` 是旧 MemoryStore 配置，不会开启 Agent 持久化。

## 能力亮点

| 图标 | 能力 | 说明 |
| --- | --- | --- |
| 🔁 | **Agent 循环** | 手写 Planner → Executor → Observation → Reflection，最多 8 轮，显式 `Finish` / `call_tool` 合约 |
| 🧰 | **工具注册表** | 内置运维工具、SCM REST 工具与 MCP 共用一条分发路径 |
| 🧩 | **MCP server** | `metrics.get`、`logs.search`、`git.diff`，MCP 形状结果；`mcp` 为可选依赖 |
| 🛡️ | **Policy** | 读 ⇒ allow，写 ⇒ 需审批，未知 ⇒ deny；跨租户写直接拒绝 |
| ✋ | **持久化 HITL** | 单次使用审批单与挂起 `AgentState`；`AGENT_PG_DSN` 指向 `db_agent` 时可跨重启 |
| 🧠 | **记忆** | 五层记忆 + 真实 Episodic 检索（任务类型、工具、词面相似度）；排序前先做租户隔离 |
| 📚 | **检索** | 确定性 hashing embedding + 内存余弦索引 + 词面/向量加权 rerank；pgvector 可选 |
| 🔀 | **工作流** | 手写 durable guard（timeout / retry / idempotency / loop limit）+ 可选真 LangGraph `StateGraph` |
| 📉 | **可观测** | 内存 Tracer，可选 Prometheus `/metrics`，可选 OTel span 适配 |
| 🧪 | **场景** | IT 运维、合同合规、销售分析使用真实 dict/JSON fixture；缺数据返回 `insufficient_data` |
| 🧾 | **Agent 任务** | 状态机、token 用量、按模型计价、状态历史环形缓冲 |
| 🔒 | **安全** | 明确 Policy 层；不把原始异常文本直接返回；租户隔离 repository |

## 已实现 / 需要条件才生效

| 状态 | 能力 | 激活条件 | 验证状态 |
| --- | --- | --- | --- |
| **始终可用** | 手写 runtime、Tool Registry、Policy/HITL 状态机、本地 hashing RAG、内存 Episodic、内存审批/挂起任务、内存 Tracer、fixture 场景 | 只需 `pip install -e .` | CI（单测/fixture） |
| **条件生效** | 持久化审批 / 挂起任务 / Episodic / checkpoint | `AGENT_PG_DSN` 指向 `db_agent`（5432），并已执行 schema | 真实 PG `db_agent` 已手动验证；无 `AGENT_TEST_PG_DSN` 时 CI skip |
| **条件生效** | pgvector 后端 | `RAG_PGVECTOR_ENABLED=1` + 带 `vector` 扩展的 Postgres。已验证测试实例是 `192.168.0.121:5434/db_agent_vector`（`extversion=0.8.7`）；**不要用 5433**，那是另一个项目的容器 | 真实实例已手动验证（5434，vector 0.8.7）；CI 只走内存索引 |
| **条件生效** | MCP server | `pip install -e ".[mcp]"`；`python -m agent.mcp.server` 仅 stdio | 工具注册有 CI 单测；stdio 启动为手动验证，不是常驻 daemon |
| **条件生效** | Prometheus `/metrics` | `pip install -e ".[observability]"` | CI 单测覆盖 200 / 503 |
| **条件生效** | OTLP spans | `pip install -e ".[observability]"` + `OTEL_EXPORTER_OTLP_ENDPOINT`（gRPC 4317 / HTTP 4318）；未设置保持内存 Tracer | exporter 接线有 CI 单测；真实 collector 投递已手动验证（4317/4318，指标出口 8889）；无厂商后端 |
| **条件生效** | 真 LangGraph / PG checkpoint | `pip install -e ".[langgraph]"`；PG checkpoint 还需 `AGENT_PG_DSN` | 装可选组时图测试在 CI 运行；PG checkpoint 为手动验证，无 DSN 时 skip |
| **条件生效** | 真实 SCM 工具 | `SCM_GATEWAY_URL` + 认证配置；否则工具保持 mock | CI 走 mock；本仓库未验证真实 gateway |
| **未声称** | 生产 collector/告警、OTLP 后端保留、MCP HTTP daemon、kill/restart 演练 | 本仓库未实现 | — |

**验证口径：**「CI」表示每次绿色构建都会实际执行该路径；「手动」表示只在表中实例上跑过一次，CI 不会重复。手动结果不等于 CI 保证。

## 技术栈

| 层 | 组件 |
| --- | --- |
| **运行时** | Python 3.12 / 3.13、FastAPI、Uvicorn、Pydantic |
| **Agent** | 手写 planner/executor 循环、`AgentTask` 状态机、token/成本核算 |
| **LLM** | DeepSeek（OpenAI 兼容）+ 确定性 `echo` 兜底 |
| **工具** | 进程内 Tool Registry、SCM REST 适配、可选 MCP 1.x/2.x server |
| **RAG** | 本地 feature-hashing embedding、内存余弦索引、词面/向量 rerank；可选 pgvector |
| **记忆** | User / Org / Conv / Task / Episodic 五层、`MemoryStore`、租户/权限过滤 |
| **持久化** | PostgreSQL + psycopg 3 + SQLAlchemy；无 `AGENT_PG_DSN` 时 InMemory repository |
| **工作流** | 手写图 + durable guard；可选 LangGraph `StateGraph` 与 `PostgresSaver` |
| **可观测** | 内存 Tracer、可选 Prometheus、可选 OpenTelemetry span 适配 |
| **测试** | pytest；按可选依赖与 DSN 分为 183 / 187 / 192 passing 三档 |
| **CI** | GitHub Actions，Python 3.12/3.13 矩阵，`contents: read`，并发取消 |

## 架构

```
Client / curl / OpenAPI UI
        │
        ▼
FastAPI gateway — apps/api/main.py :8762
  /health · /metrics · /docs · /chat · /approval · /resume
        │
        ▼
Agent Runtime — planner → executor → observation → reflection
  AgentTask 状态机 · token/cost · 状态历史
        │
        ├── Tools   → 内置运维 / SCM REST（默认 mock）/ MCP（可选）
        ├── Context → 预算/压缩 → memory providers → episodic recall
        ├── RAG     → hashing embedding → 向量索引 → rerank → pgvector?
        ├── Policy  → allow / need_approval / deny → approval + paused stores
        └── Workflow→ durable guards → LangGraph StateGraph? → checkpoint
        │
        ▼
Observability — 内存 Tracer → Prometheus? / OpenTelemetry?
```

本仓库没有跨进程 RPC、服务注册中心或消息队列。可选外部依赖只有 Postgres、SCM、MCP、Prometheus、OpenTelemetry 与 LangGraph，全部由显式环境变量或可选包开启。

## 覆盖场景

每个场景都由下列代码实现，并用 fixture 或默认 mock 路径测试。右列写的是真实约束，不是宣传语。

| 场景 | Agent 做什么 | 约束 |
| --- | --- | --- |
| **IT 运维** | 接收故障问题，调用 metrics/logs/git，查知识库，输出有依据的诊断 | `run_it_ops`；未配置 `SCM_GATEWAY_URL` 时 SCM/运维工具走 mock |
| **合同合规** | 读取 contract dict/JSON，校验必填字段，标记风险条款与金额阈值，检索制度，并在有 supplier id 时调用供应商工具 | `review_contract`；缺数据返回 `insufficient_data`，不编造结果 |
| **销售分析** | 读取 `workflow/data/sales_week.json`，计算 GMV / 订单 / 客单价 / 支付失败 / 类目变化，再补 metrics 与知识库调用 | `analyze_sales`；缺文件或空数据返回 `insufficient_data` |

> [!NOTE]
> 三个场景是演示流程，不是生产 BI 或合同系统连接器。只有配置 `SCM_GATEWAY_URL` 才会使用真实 SCM 数据；否则 mock 是文档化默认行为。

## 包结构

| 包 | 职责 |
| --- | --- |
| `apps/api` | FastAPI 网关、`/chat`、`/approval`、`/resume`、`/metrics`、启动接线 |
| `agent/runtime` | Planner/Executor 循环、`AgentState`、`AgentTask`、挂起任务持久化 |
| `agent/tools` | Tool Registry、guarded executor、内置运维工具 |
| `agent/mcp` | 可选 MCP server 与纯函数 spec/call helper |
| `agent/context` | 预算上下文组装、压缩、memory provider |
| `agent/memory` | 五层 `MemoryStore` 与 `EpisodicMemoryStore` |
| `knowledge` | 文档、切分、hashing embedding、向量索引、rerank、pgvector 可选 |
| `security` | `PolicyEngine`、`ApprovalStore`、InMemory/Postgres repository |
| `workflow` | 手写图、durable guard、checkpoint 适配、场景与 fixture |
| `observability` | 内存 Tracer、Prometheus metrics、OTel 适配 |
| `infrastructure/pg` | connector 抽象、Postgres/InMemory 实现、DDL |
| `evaluation` | 基于用例的 evaluator |
| `scripts/db` | `db_agent` 的 `agent_schema.sql` |
| `tests` | 离线测试 + opt-in Postgres/pgvector 集成测试 |

### 目录结构

```
cognora-enterprise-agent/
├── apps/api/                # FastAPI 网关
├── agent/
│   ├── runtime/             # loop · state · agent_task · paused_tasks
│   ├── tools/               # registry · executor · builtin tools
│   ├── mcp/                 # 可选 MCP server
│   ├── context/             # assembler、budget、compression、memory providers
│   └── memory/              # store + episodic
├── knowledge/               # embedding · vector_index · store · pgvector
├── security/                # policy · approvals
├── workflow/                # graph · checkpoint · scenarios · data/
├── observability/           # tracing · metrics · otel
├── infrastructure/pg/       # connector · schema
├── evaluation/              # evaluator
├── scripts/db/              # agent_schema.sql
├── tests/                   # 离线 + opt-in 集成测试
└── docs/superpowers/        # 设计规格与实现计划
```

## 关键架构模式

### 持久化 HITL 与精确匹配审批

写操作不能因为用户“说了”就执行。`PolicyEngine` 对读返回 `allow`，对写返回 `need_approval`，对未知或跨租户操作返回 `deny`。特权 executor 只在审批单为 `approved`、未被消费、且存储的 `tool` + `args` 与待执行动作完全匹配时才放行。`POST /approval/{id}/decide` 记录决策；`POST /chat/{task_id}/resume` 执行这一个已批准动作，被驳回则取消任务。

配置 `AGENT_PG_DSN` 后，审批单落 `agent_approval`，完整 `AgentState` 快照（含 `AgentTask`）落 `agent_paused_task`，按 `(tenant_id, task_id)` 隔离。没有 DSN 时只在进程内；重启会丢失挂起审批，但离线测试永远不需要数据库。

### Episodic 记忆

每次 `AgentTask` 状态迁移都会追加到 `memory_episodic`。检索按任务类型、工具名与 2-gram/关键词重合度打分，排序前先按租户过滤。它刻意不依赖外部向量库；词面路径确定性强、可离线测试。

### 带硬过滤的检索

`KnowledgeStore` 在 embedding 与向量打分之前先按租户和权限过滤候选。默认 embedding provider 是本地 feature hashing，因此 `pip install -e .` 不需要下载模型，也没有网络依赖。`pgvector` 是 `RAG_PGVECTOR_ENABLED=1` 后面的可选后端；扩展或 DSN 缺失时回退内存 store。

真实 pgvector 实例已验证：`192.168.0.121:5434/db_agent_vector`，扩展 `vector 0.8.7`。`tests/test_rag.py` 在该库 **7/7 通过**，向量命中测试首先返回运维文档。不要复用 **5433**，它被另一个项目占用。主持久化库仍是 `192.168.0.121:5432/db_agent`，它没有 `vector` 扩展，因此那里的 pgvector 测试按设计 skip。

### 安装后才启用的真 LangGraph

安装了 `langgraph` 且 checkpointer 是原生实现时，`workflow/graph_lang.py` 运行真正的 `StateGraph`；否则走诚实的模拟路径。LangGraph 1.x 用 `Command(resume=...)` 暂停/恢复，`PostgresSaver` checkpoint 落 `db_agent`。手写的 `workflow/graph.py` 仍是零依赖路径。

### MCP 只支持 stdio，刻意不做常驻服务

`python -m agent.mcp.server` 以 stdio 启动 MCP server。它刻意**不是**常驻 HTTP 服务：MCP 客户端把进程作为子进程拉起，通过 stdin/stdout 通信，因此没有端口、没有 daemon，也没有后台服务需要部署。若要改成长生命周期 HTTP 形态，需要另行设计会话、认证与生命周期；当前发布不声称已经部署常驻 MCP 服务。

### 不绑定厂商的可观测性

内存 `Tracer` 始终记录任务生命周期。安装 `prometheus-client` 后，`GET /metrics` 输出任务状态、工具调用、trace 事件计数。安装 `opentelemetry-*` 与 `opentelemetry-exporter-otlp` **且** 配置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后，同一批事件通过真实 `SimpleSpanProcessor` / OTLP exporter 写 span（gRPC 4317 或 HTTP 4318；`_protocol_for` 按端口选择）。否则代码会记录走哪条路径并保留内存 Tracer；缺包或缺 endpoint 绝不影响启动。endpoint 不是密钥，但日志里不会出现 DSN/密码。

验证 collector 接受了 OTLP metrics：`curl http://192.168.0.121:8889/metrics` 暴露 `agent_verify_points_total{result="ok"} 3`（HTTP）与 `agent_verify_grpc_points_total{result="ok"} 7`（gRPC）。该 collector 只是本地 HTTP/gRPC receiver + Prometheus exporter + debug 输出，没有外部后端、保留或告警，不声称是生产部署。

## 运维

### 持久化

`AGENT_PG_DSN` 是审批、挂起任务、Episodic 记忆与 checkpoint 的唯一开关。建独立库并执行幂等 schema：

```bash
psql "postgresql://<user>:<password>@192.168.0.121:5432/postgres" \
     -c "CREATE DATABASE db_agent OWNER <user>;"

export AGENT_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5432/db_agent"
psql "$AGENT_PG_DSN" -f scripts/db/agent_schema.sql
```

如果本机没有 `psql`，可以用任意 Postgres 客户端（例如 `psycopg`）执行同一份 DDL。脚本会创建 `agent_approval`、`agent_paused_task` 与全部 `memory_*` 表。LangGraph 的 `PostgresSaver.setup()` 首次使用时创建 `checkpoints` / `checkpoint_*`。

### CI 与发布

`.github/workflows/ci.yml` 在 `master` 上跑 Python **3.12 / 3.13** 矩阵，`pip install -e .` 后执行 `pytest -q -rs`；并发取消重复运行，权限最小化为 `contents: read`。CI 刻意不配置 `AGENT_TEST_PG_DSN`、`DEEPSEEK_API_KEY` 或 `SCM_GATEWAY_URL`，所以 Postgres/pgvector 集成测试与可选依赖探测按设计 skip；离线套件必须全绿。

`master` 已启用 branch protection：要求 PR、禁止强推、禁止删除分支，不强制 review 数。`v0.5.1` 是附注 tag，Release 从 tag 创建。

### 测试

```bash
# 裸装（无可选依赖、无 DSN）
pip install -e . && pytest -q
# 183 passed, 8 skipped

# 可选依赖，仍无 DSN
pip install -e ".[mcp,observability,langgraph]" && pytest -q
# 187 passed, 6 skipped

# 真实 Postgres 集成（db_agent；无 vector 扩展）
export AGENT_TEST_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5432/db_agent"
pytest -q
# 192 passed, 1 skipped（pgvector）

# 真实 pgvector 集成（db_agent_vector；vector 0.8.7）
export AGENT_TEST_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5434/db_agent_vector"
pytest -q
# 193 passed, 0 skipped
```

`db_agent` 的 skip 是 pgvector：该 Postgres 镜像没有 `vector` 扩展。在 `db_agent_vector` 上全套测试全绿，且 `tests/test_rag.py` 为 **7 passed, 0 skipped**。两个库验证后都已清理（`agent_approval`、`agent_paused_task`、`memory_*`、`agent_knowledge_chunk` 均为 0 行）。

## FAQ

<details>
<summary><strong>不配 Postgres 或模型 key 能跑吗？</strong></summary>

能。没有 `AGENT_PG_DSN` 时审批与挂起任务在进程内存，Episodic 记忆是进程内列表。没有 `DEEPSEEK_API_KEY` 时 planner 退回 `echo`，所有工具维持 mock。`pip install -e . && pytest -q` 就是预期离线路径。

</details>

<details>
<summary><strong>为什么 <code>GET /metrics</code> 返回 503？</strong></summary>

`prometheus-client` 是可选依赖。执行 `pip install -e ".[observability]"` 后才渲染 Prometheus 文本；没有安装时返回 503 并说明原因，而不是让启动失败。

</details>

<details>
<summary><strong>为什么我的图跑的是模拟路径？</strong></summary>

真 `StateGraph` 需要可选 `langgraph` 组 **且** 传入原生 checkpointer。两者缺一，代码就刻意走手写模拟路径。安装 `pip install -e ".[langgraph]"` 并传入 `get_checkpointer(...)` 才会走真图。

</details>

<details>
<summary><strong>为什么 Postgres 测试被 skip？</strong></summary>

它们由 `AGENT_TEST_PG_DSN` 门控。指向可达的 `db_agent` 后运行 `pytest -q`；pgvector 测试还要求 `vector` 扩展。应用本身不读取 `AGENT_TEST_PG_DSN`。

</details>

<details>
<summary><strong>为什么所有 SCM 工具都返回 mock？</strong></summary>

因为 `SCM_GATEWAY_URL` 未设置。这是文档化默认行为，不是故障。配置网关 URL（以及认证参数）后工具才切到真实 REST。

</details>

## 文档

- [AGENTS.md](./AGENTS.md) — 仓库工作说明、架构不变量、构建/测试命令
- [CHANGELOG.md](./CHANGELOG.md) — 版本历史与激活条件
- [CONTRIBUTING.md](./CONTRIBUTING.md) — 离线测试规则、可选依赖策略、PR 流程
- [docs/superpowers/plans/2026-09-15-enterprise-agent-roadmap.md](./docs/superpowers/plans/2026-09-15-enterprise-agent-roadmap.md) — 阶段路线图
- [docs/superpowers/plans/2026-09-16-v0.5.0-roadmap.md](./docs/superpowers/plans/2026-09-16-v0.5.0-roadmap.md) — 后续候选
- [SECURITY.md](./SECURITY.md) — 漏洞报告
- [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — 社区约定

漏洞请通过 [SECURITY.md](./SECURITY.md) 报告。

## 相关项目

- **[scm-platform](https://github.com/dengxingzhi000/scm-platform)** — 本 Agent 在配置 `SCM_GATEWAY_URL` 后可调用的 Java/Spring Cloud 供应链平台。
- **[enterprise-agent](https://github.com/dengxingzhi000/enterprise-agent)** — 本 Agent 早期历史的原始仓库。

## 贡献

欢迎参与！可以在 [Issues](https://github.com/dengxingzhi000/cognora-enterprise-agent/issues)
报告 bug 或提功能建议，也可以直接发 PR。

请先读 [CONTRIBUTING.md](./CONTRIBUTING.md)，其中包含离线优先测试规则、可选依赖策略与 CI 检查项。

1. Fork 仓库
2. 创建分支（`git checkout -b feature/amazing-feature`）
3. 提交（`git commit -m 'feat: add amazing feature'`）
4. 推送（`git push origin feature/amazing-feature`）
5. 发起 Pull Request

所有重要变更记录在 [CHANGELOG.md](./CHANGELOG.md)。参与者需遵守 [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)。

## License

[Apache License 2.0](./LICENSE)

---

<div align="center">

**为可运维的企业 AI Agent 而写，离线路径真的能跑。**

</div>
