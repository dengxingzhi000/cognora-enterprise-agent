"""Stage 1b Gateway - /chat routes into Agent Runtime."""
import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from agent.assistance.knowledge import AssistanceKnowledgeService
from agent.assistance.predictor import AssistancePredictor
from agent.assistance.service import AssistanceService
from agent.assistance.store import build_assistance_store
from agent.context.assembler import ContextAssembler
from agent.context.budget import TokenBudget
from agent.memory.conversation import build_conversation_store, new_session_id
from agent.runtime import loop as _runtime_loop
from agent.runtime.llm_planner import (
    build_deepseek_planner,
    get_default_client,
    planner_prompt_tokens,
)
from agent.runtime.paused_tasks import build_paused_task_store
from agent.runtime.state import AgentState
from agent.runtime.writing_assistant import (
    SensitiveContentError,
    WritingAssistanceRequest,
    WritingOutputError,
    estimate_writing_tokens,
    generate_writing,
    reject_sensitive_credentials,
)
from agent.tools.executor import (
    ToolTimeout,
    _registry,
    execute_with_timeout,
    guarded_executor,
    tool_timeout_seconds,
)
from apps.api.assistance_router import create_assistance_router
from apps.api.async_tasks import AsyncTaskQueue, TaskQueueFull, build_task_queue
from apps.api.knowledge_router import create_knowledge_router
from apps.api.task_router import create_task_router
from apps.api.workflow_router import (
    create_expense_workflow_router,
    create_workflow_registry_router,
)
from infrastructure.pg.connector import (
    get_agent_connector,
    get_connector,
    strict_persistence_enabled,
)
from infrastructure.pg.schema import ensure_schema
from integrations.scm.client import scm_trace_id
from integrations.scm.context import scm_identity_context
from knowledge.pgvector_store import pgvector_enabled
from knowledge.service import get_knowledge_service
from observability import metrics as _metrics
from observability.otel import build_otel_adapter
from observability.trace_context import bind_traceparent, new_traceparent, reset_traceparent
from observability.trace_store import build_trace_store
from observability.tracing import Tracer, configure_trace_store
from security.approval import (
    ApprovalConflict,
    ApprovalStore,
    approval_lease_seconds,
    build_approval_store,
    reclaim_on_resume_enabled,
    start_lease_heartbeat,
    validate_heartbeat_settings,
)
from security.internal_auth import (
    InternalSignatureMiddleware,
    TrustedIdentity,
    trusted_identity,
    validate_internal_signature_config,
)
from security.policy import PolicyEngine
from security.quota import QuotaExceeded, build_quota_guard
from security.tool_effect import (
    CACHED as TOOL_EFFECT_CACHED,
)
from security.tool_effect import (
    CONFLICT as TOOL_EFFECT_CONFLICT,
)
from security.tool_effect import (
    MANUAL_RECONCILIATION_ERROR,
    build_tool_effect_store,
    result_ref,
)
from workflow.checkpoint import get_checkpointer
from workflow.registry import WorkflowRegistry

if "pytest" not in sys.modules and not os.getenv("PYTEST_CURRENT_TEST"):
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

app = FastAPI(title="Enterprise Agent Gateway", version="0.5.1")
app.add_middleware(InternalSignatureMiddleware)

_KNOWLEDGE = None
_WORKFLOWS: WorkflowRegistry | None = None


def _get_knowledge_service():
    global _KNOWLEDGE
    if _KNOWLEDGE is None:
        _KNOWLEDGE = get_knowledge_service()
    return _KNOWLEDGE


def _get_workflow_registry() -> WorkflowRegistry:
    global _WORKFLOWS
    if _WORKFLOWS is None:
        _WORKFLOWS = WorkflowRegistry()
    return _WORKFLOWS


app.include_router(create_knowledge_router(_get_knowledge_service))
app.include_router(create_workflow_registry_router(_get_workflow_registry))
app.include_router(create_expense_workflow_router(
    lambda: _APPROVALS,
    lambda: getattr(app.state, "workflow_checkpointer", _WORKFLOW_CHECKPOINTER),
))
app.include_router(create_task_router(lambda: getattr(app.state, "task_queue", _TASK_QUEUE)))

def _run_durable_job(kind: str, payload: dict):
    if kind != "chat":
        raise ValueError("unsupported durable job")
    req = ChatRequest.model_validate(payload["request"])
    identity = TrustedIdentity(
        app_id=str(payload["app_id"]), tenant_id=str(payload["tenant_id"]),
        user_id=str(payload["user_id"]), role=str(payload["role"]),
    )
    with scm_identity_context(identity):
        return _chat(req, identity)


# HITL 状态：审批单与挂起任务用 repository 抽象。
# AGENT_PG_DSN 是唯一开关；有值走 Postgres，否则自动回落 InMemory，
# 测试与本地开发不需要数据库。挂起任务按 (tenant_id, task_id) 隔离。
_APPROVALS: ApprovalStore = build_approval_store()
_PAUSED_TASKS = build_paused_task_store()
_TOOL_EFFECTS = build_tool_effect_store()
_WRITING_QUOTA = build_quota_guard()
_CHAT_QUOTA = build_quota_guard()
_CONVERSATIONS = build_conversation_store()
_TASK_QUEUE: AsyncTaskQueue = build_task_queue(job_runner=_run_durable_job)
_ASSEMBLER = ContextAssembler()
_WORKFLOW_CHECKPOINTER: Any = MemorySaver()
_ASSISTANCE_STORE = build_assistance_store(None)
_ASSISTANCE_PREDICTOR = AssistancePredictor(model_dir=os.getenv("AGENT_ASSISTANCE_MODEL_DIR") or None)
_ASSISTANCE_KNOWLEDGE: AssistanceKnowledgeService | None = None


def _get_assistance_service() -> AssistanceService:
    global _ASSISTANCE_KNOWLEDGE
    if _ASSISTANCE_KNOWLEDGE is None:
        _ASSISTANCE_KNOWLEDGE = AssistanceKnowledgeService(_get_knowledge_service())
    return AssistanceService(_ASSISTANCE_STORE, _ASSISTANCE_PREDICTOR, knowledge=_ASSISTANCE_KNOWLEDGE)


app.include_router(create_assistance_router(_get_assistance_service))


class ChatRequest(BaseModel):
    message: str
    user_id: str = "u1"
    tenant_id: str = "t1"
    task_id: str | None = None
    session_id: str | None = None
    assistance_prediction_id: str | None = None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str = "unknown"
    tenant_id: str = "t1"


class ResumeRequest(BaseModel):
    tenant_id: str = "t1"


@app.get("/health")
def health():
    return {"status": "ok", "stage": 1}


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _validate_required_deepseek() -> None:
    if not _env_enabled("AGENT_REQUIRE_DEEPSEEK"):
        return
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-xxxx"):
        raise RuntimeError(
            "AGENT_REQUIRE_DEEPSEEK=true requires a non-placeholder DEEPSEEK_API_KEY"
        )
    client, _ = get_default_client()
    if client is None:
        raise RuntimeError(
            "AGENT_REQUIRE_DEEPSEEK=true requires a non-placeholder DEEPSEEK_API_KEY"
        )


def _dependency_ready(name: str, check) -> bool:
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 - readiness reports status, never dependency internals
        logger.warning("readiness dependency %s is unavailable", name, exc_info=True)
        return False


@app.get("/ready")
def readiness():
    """Readiness checks required runtime dependencies without replacing liveness."""
    dependencies: dict[str, bool] = {}
    if strict_persistence_enabled():
        connector = getattr(app.state, "agent_connector", None)
        dependencies["postgres"] = _dependency_ready(
            "postgres", lambda: connector is not None and connector.healthcheck()
        )
    if _env_enabled("AGENT_REQUIRE_DEEPSEEK"):
        dependencies["deepseek"] = _dependency_ready(
            "deepseek", lambda: get_default_client()[0] is not None
        )
    if _env_enabled("AGENT_REQUIRE_ASSISTANCE_MODEL"):
        dependencies["assistance_model"] = _dependency_ready(
            "assistance_model", _ASSISTANCE_PREDICTOR.is_ready
        )
    if not all(dependencies.values()):
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependencies": dependencies},
        )
    return {"status": "ready", "dependencies": dependencies}


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus 文本指标，由官方 prometheus-client registry 渲染。"""
    try:
        payload, content_type = _metrics.render_metrics()
    except _metrics.MetricsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    return Response(content=payload, media_type=content_type)


@app.get("/capabilities")
def capabilities():
    """Stable capability discovery for the trusted platform-agent BFF."""
    return {
        "tools": _registry().list_tools(),
        "knowledge": True,
        "memory": True,
        "workflows": True,
        # MCP is an optional integration; true means the capability is available
        # to enable, not that the optional package is installed in this process.
        "mcp": True,
        "mcpInstalled": importlib.util.find_spec("mcp") is not None,
        "writingAssistant": get_default_client()[0] is not None,
    }


@app.on_event("startup")
def _startup():
    global _ASSISTANCE_STORE
    validate_internal_signature_config()
    # 余量约束不满足直接启动失败：租约保护失效属于正确性问题，不接受静默降级。
    validate_heartbeat_settings()
    _validate_required_deepseek()
    strict_persistence = strict_persistence_enabled()
    if strict_persistence and not pgvector_enabled():
        raise RuntimeError("AGENT_STRICT_PERSISTENCE=true requires RAG_PGVECTOR_ENABLED=1")
    app.state.agent_connector = get_agent_connector()
    if app.state.agent_connector is None and not strict_persistence:
        app.state.agent_connector = get_connector()
    try:
        ensure_schema(app.state.agent_connector)
    except Exception as e:
        if strict_persistence:
            raise
        import warnings
        warnings.warn(f"memory schema init failed: {e}", stacklevel=2)
    persistent = (app.state.agent_connector
                  if getattr(app.state.agent_connector, "_engine", None) is not None else None)
    _ASSISTANCE_STORE = build_assistance_store(persistent)
    app.state.writing_quota = build_quota_guard(persistent)
    app.state.conversations = build_conversation_store(persistent)
    app.state.task_queue = build_task_queue(persistent, job_runner=_run_durable_job)
    app.state.task_queue.fail_running()
    app.state.chat_quota = build_quota_guard(persistent)
    app.state.assembler = ContextAssembler(
        budget=TokenBudget(
            total=_positive_env_int("CONTEXT_BUDGET_TOKENS", 8000),
            reserved_for_output=_positive_env_int("CONTEXT_RESERVED_TOKENS", 1000),
        ),
        conversation_keep_recent=_positive_env_int("CONVERSATION_KEEP_RECENT", 3),
        observation_keep_recent=_positive_env_int("OBSERVATION_KEEP_RECENT", 5),
    )
    configure_trace_store(build_trace_store(persistent))
    app.state.workflow_checkpointer = get_checkpointer() if persistent else _WORKFLOW_CHECKPOINTER
    try:
        Tracer.otel = build_otel_adapter()
    except Exception as e:  # noqa: BLE001 - 可观测性不能阻断启动
        import warnings
        warnings.warn(f"OTel adapter init failed: {e}", stacklevel=2)
    # 启动时随惰性回收一起清理崩溃遗留的 started effect，不新增长驻线程。
    _reclaim_stale_tool_effects()


def _mark_approval_failed(approval_id: str, claim_token: str, summary: str) -> None:
    """工具/执行失败时把审批置为 failed；摘要已脱敏，异常全文只进日志。"""
    try:
        _APPROVALS.fail(approval_id, claim_token, summary)
    except Exception:  # noqa: BLE001 - 标记失败不能掩盖原始错误
        logger.exception("failed to mark approval %s as failed", approval_id)


def _reclaim_stale_tool_effects() -> int:
    """回收崩溃遗留的 started effect；approval 仍有活跃租约时绝不回收。"""
    try:
        count = _TOOL_EFFECTS.reclaim_stale_started(approvals=_APPROVALS)
        if count:
            logger.warning("reclaimed %d stale started tool effect(s)", count)
        return count
    except Exception:  # noqa: BLE001 - 回收失败不阻断启动/本次 resume
        logger.exception("stale tool effect reclaim failed")
        return 0


def _chat_payload(final: AgentState | None, user_id: str, trace_id: str,
                  session_id: str | None = None) -> dict:
    at = final.context.get("agent_task") if final else None
    payload = {
        "reply": final.answer if final else "",
        "status": final.status if final else "failed",
        "user_id": user_id,
        "task_id": at.task_id if at else None,
        "trace_id": trace_id,
        "session_id": session_id,
    }
    if at and final and final.status != "failed":
        payload["token_usage"] = dict(at.token_usage)
        payload["cost"] = at.cost

    # v2 Phase 3: HITL 闭环 — paused + need_approval 时把 aid 暴露给 caller
    if final and final.status == "paused" and final.context.get("pause_reason") == "need_approval":
        payload["approval_id"] = final.context.get("approval_id")
        payload["approval_tool"] = final.context.get("approval_tool")
        payload["approval_args"] = final.context.get("approval_args")
    return payload


@app.post("/chat")
def chat(req: ChatRequest, request: Request):
    identity = trusted_identity(request)
    with scm_identity_context(identity):
        return _chat(req, identity)


def _chat(req: ChatRequest, identity):
    started = time.perf_counter()
    tenant_id = identity.tenant_id if identity else req.tenant_id
    user_id = identity.user_id if identity else req.user_id
    role = identity.role if identity else "admin"
    quota = getattr(app.state, "chat_quota", _CHAT_QUOTA)
    client, model = get_default_client()
    planner = build_deepseek_planner(client=client, model=model)
    context_budget = _positive_env_int("CONTEXT_BUDGET_TOKENS", 8000)
    planner_output = _positive_env_int("AGENT_PLANNER_MAX_TOKENS", 300)
    per_call_input = context_budget + len(req.message) // 4 + planner_prompt_tokens(client)
    estimated_tokens = (
        8 * (per_call_input + planner_output)
        if client is not None else max(1, len(req.message) // 4)
    )
    try:
        quota.check_request(tenant_id)
        quota.consume_tokens(tenant_id, estimated_tokens)
    except QuotaExceeded:
        raise HTTPException(status_code=429, detail={"code": "AGENT_QUOTA_EXCEEDED"}) from None

    session_id = req.session_id or new_session_id()
    conversations = getattr(app.state, "conversations", _CONVERSATIONS)
    history = conversations.list(session_id, tenant_id, user_id, limit=50) if req.session_id else []
    st = AgentState(
        task=req.message,
        messages=[{"role": item.role, "content": item.content} for item in history],
    )
    if req.task_id:
        st.task_id = req.task_id
    st.context["tenant_id"] = tenant_id
    st.context["session_id"] = session_id

    user = {"tenant_id": tenant_id, "role": role, "user_id": user_id}
    st.context["user"] = user

    tracer = Tracer()
    tid = tracer.start_trace(req.message, tenant_id=tenant_id)
    st.context["trace_id"] = tid
    cv_token = scm_trace_id.set(tid)
    trace_token = bind_traceparent(new_traceparent())
    final = None
    try:
        final = _runtime_loop.run(
            st, planner=planner,
            assembler=getattr(app.state, "assembler", _ASSEMBLER),
            executor=lambda plan, state: guarded_executor(
                plan, state, user=user,
                policy=PolicyEngine(),
                approvals=_APPROVALS,
            ),
        )
    finally:
        status = final.status if final is not None else "unknown"
        tracer.end_trace(tid, status=status)
        scm_trace_id.reset(cv_token)
        reset_traceparent(trace_token)

    at = final.context.get("agent_task") if final else None
    if at and final.status == "paused":
        _PAUSED_TASKS.save(tenant_id, at.task_id, final)

    payload = _chat_payload(final, user_id, tid, session_id)
    conversations.append(session_id, tenant_id, user_id, "user", req.message)
    conversations.append(session_id, tenant_id, user_id, "assistant", payload["reply"])
    if req.assistance_prediction_id and final and final.status in {"done", "failed"}:
        try:
            _get_assistance_service().record_prediction_outcome(
                tenant_id,
                user_id,
                req.assistance_prediction_id,
                "TASK_COMPLETED" if final.status == "done" else "TASK_FAILED",
                int((time.perf_counter() - started) * 1000),
            )
        except Exception:  # noqa: BLE001 - outcome telemetry must not break chat
            logger.warning("assistance outcome recording failed", exc_info=True)
    return payload


@app.post("/chat/async")
def chat_async(req: ChatRequest, request: Request):
    identity = trusted_identity(request)
    queue = getattr(app.state, "task_queue", _TASK_QUEUE)
    tenant_id = identity.tenant_id if identity else req.tenant_id
    user_id = identity.user_id if identity else req.user_id
    role = identity.role if identity else "admin"
    job = {
        "kind": "chat",
        "payload": {
            "request": req.model_dump(mode="json"),
            "app_id": identity.app_id if identity else "standalone",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": role,
        },
    }
    try:
        task_id = queue.submit(
            tenant_id, user_id, req.session_id, None,
            external_task_id=req.task_id, job=job,
        )
    except TaskQueueFull:
        raise HTTPException(status_code=503, detail={"code": "AGENT_TASK_QUEUE_FULL"}) from None
    return {"task_id": task_id, "status": "queued"}


@app.get("/chat/sessions/{session_id}")
def chat_history(session_id: str, request: Request):
    identity = trusted_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="trusted identity required")
    conversations = getattr(app.state, "conversations", _CONVERSATIONS)
    return {"messages": [
        {"role": item.role, "content": item.content, "created_at": item.created_at}
        for item in conversations.list(session_id, identity.tenant_id, identity.user_id)
    ]}


@app.post("/writing-assistant")
def writing_assistant(req: WritingAssistanceRequest, request: Request):
    identity = trusted_identity(request)
    tenant_id = identity.tenant_id if identity else "standalone"
    try:
        reject_sensitive_credentials(req)
    except SensitiveContentError:
        raise HTTPException(
            status_code=422,
            detail={"code": "SENSITIVE_CONTENT_REJECTED"},
        ) from None
    client, model = get_default_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "WRITING_ASSISTANT_UNAVAILABLE"},
        )
    quota = getattr(app.state, "writing_quota", _WRITING_QUOTA)
    estimated_tokens = estimate_writing_tokens(req)
    try:
        quota.check_request(tenant_id)
        quota.consume_tokens(tenant_id, estimated_tokens)
    except QuotaExceeded:
        raise HTTPException(
            status_code=429,
            detail={"code": "WRITING_RATE_LIMITED"},
        ) from None
    try:
        result = generate_writing(req, client=client, model=model)
    except SensitiveContentError:
        raise HTTPException(
            status_code=422,
            detail={"code": "SENSITIVE_CONTENT_REJECTED"},
        ) from None
    except WritingOutputError:
        raise HTTPException(
            status_code=502,
            detail={"code": "WRITING_OUTPUT_REJECTED"},
        ) from None
    except Exception as exc:
        logger.warning("writing assistant provider call failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail={"code": "WRITING_PROVIDER_FAILED"},
        ) from None
    return result


@app.post("/approval/{approval_id}/decide")
def decide_approval(approval_id: str, req: ApprovalDecision, request: Request):
    """人工审批：approve / reject。只记录决定，不触发执行。"""
    identity = trusted_identity(request)
    tenant_id = identity.tenant_id if identity else req.tenant_id
    approver = identity.user_id if identity else req.approver
    try:
        item = _APPROVALS.get(approval_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None

    if item["user"].get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    if identity is not None and identity.role not in ("admin", "manager", "finance"):
        raise HTTPException(status_code=403, detail="approval role not allowed")
    if item["user"].get("user_id") == approver:
        raise HTTPException(status_code=403, detail="self approval is not allowed")
    if item["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval already {item['status']}")

    if req.decision == "approve":
        try:
            item = _APPROVALS.approve(approval_id, approver)
        except ApprovalConflict:
            raise HTTPException(status_code=409, detail="approval changed concurrently") from None
    else:
        try:
            item = _APPROVALS.reject(approval_id, approver)
        except ApprovalConflict:
            raise HTTPException(status_code=409, detail="approval changed concurrently") from None

    return {
        "approval_id": approval_id,
        "status": item["status"],
        "tool": item["tool"],
        "args": item["args"],
        "approver": item.get("approver"),
    }


@app.post("/chat/{task_id}/resume")
def resume_chat(task_id: str, request: Request, req: ResumeRequest | None = None):
    """从 paused 恢复：审批通过则执行被挂起的动作，驳回则取消任务。"""
    identity = trusted_identity(request)
    with scm_identity_context(identity):
        return _resume_chat(task_id, req, identity)


def _resume_chat(task_id: str, req: ResumeRequest | None, identity):
    tenant = identity.tenant_id if identity else (req.tenant_id if req else "t1")
    # 惰性回收：resume 是审批单的唯一消费方，无需额外后台任务；一次条件 UPDATE
    # 把租约过期的 in_progress 退回 approved（崩溃后最多重放一次的前提）。
    if reclaim_on_resume_enabled():
        try:
            reclaimed = _APPROVALS.reclaim_expired_claims()
            if reclaimed:
                logger.warning("reclaimed %d expired approval claim(s)", reclaimed)
        except Exception:  # noqa: BLE001 - 回收失败不阻断本次 resume
            logger.exception("approval lease reclaim failed")
        # 先回收审批租约，再回收 stale effect：后者的「无活跃租约」条件因此成立。
        _reclaim_stale_tool_effects()
    state = _PAUSED_TASKS.get(tenant, task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="task not found or already completed")
    if state.status != "paused":
        raise HTTPException(status_code=409, detail="task is not paused")
    if state.context.get("tenant_id") != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    paused_user = state.context.get("user") or {}
    if identity is not None and paused_user.get("user_id") != identity.user_id:
        raise HTTPException(status_code=403, detail="user mismatch")
    if state.context.get("pause_reason") != "need_approval":
        raise HTTPException(status_code=409, detail="unsupported pause reason")

    aid = state.context.get("approval_id")
    if not aid:
        raise HTTPException(status_code=409, detail="no approval attached to task")
    try:
        item = _APPROVALS.get(aid)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found") from None
    if item["user"].get("tenant_id") != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")

    user = (
        {"tenant_id": tenant, "user_id": identity.user_id, "role": identity.role}
        if identity is not None
        else (state.context.get("user") or {"tenant_id": tenant, "role": "admin"})
    )
    trace_id = state.context.get("trace_id")
    at = state.context.get("agent_task")

    if item["status"] == "rejected":
        if at is not None and at.status == "paused":
            at.transition("cancelled", "approval_rejected")
        state.status = "cancelled"
        state.answer = f"approval {aid} rejected by {item.get('approver', 'unknown')}"
        _PAUSED_TASKS.delete(tenant, task_id)
        return {
            "reply": state.answer,
            "status": "cancelled",
            "user_id": user.get("user_id", "unknown"),
            "task_id": task_id,
            "trace_id": trace_id,
            "approval_id": aid,
            "approval_tool": item["tool"],
            "approval_args": item["args"],
            "executed": False,
        }

    if item["status"] != "approved":
        raise HTTPException(status_code=409, detail=f"approval is {item['status']}, not approved")

    # 两阶段第一步：原子 claim。并发 resume 只有一个进入执行，其余拿到 409；
    # 进程崩溃后行停在 in_progress，心跳停止 → 租约到期才允许重试（最多重放一次）。
    lease = approval_lease_seconds()
    try:
        claimed = _APPROVALS.claim(aid, lease)
    except ApprovalConflict:
        raise HTTPException(status_code=409, detail="approval is already claimed") from None
    token = claimed["claim_token"]

    tool = item["tool"]
    args = item["args"]

    # 写工具幂等：同一 approval + 工具 + 参数只真正执行一次。占位先于心跳；
    # succeeded 直接回放引用，started 说明上一次执行仍在进行/已崩溃（绝不并跑）。
    effect = _TOOL_EFFECTS.begin(tenant, aid, tool, args)
    if effect["decision"] == TOOL_EFFECT_CONFLICT:
        if effect.get("reason") == MANUAL_RECONCILIATION_ERROR:
            _mark_approval_failed(aid, token, MANUAL_RECONCILIATION_ERROR)
            raise HTTPException(status_code=409, detail=MANUAL_RECONCILIATION_ERROR)
        # 尚未执行任何副作用：把 claim 退回 approved，避免白等一个租约。
        try:
            _APPROVALS.release(aid, token)
        except Exception:  # noqa: BLE001 - 释放失败不掩盖冲突本身
            logger.exception("failed to release approval %s after effect conflict", aid)
        raise HTTPException(status_code=409, detail="tool execution already started")

    cached = effect["decision"] == TOOL_EFFECT_CACHED
    # 两阶段执行期：心跳每隔 lease/3 续期一次，租约因此不再需要大于工具时长。
    heartbeat = None if cached else start_lease_heartbeat(_APPROVALS, aid, token, lease)
    if cached:
        obs = {"tool": tool, "result": effect.get("result_ref") or "cached",
               "cached": True, "outcome": "success"}
    else:
        timeout = tool_timeout_seconds()
        try:
            obs = execute_with_timeout(
                lambda: guarded_executor(
                    {"action": "call_tool", "tool": tool, "args": args},
                    state, user=user, policy=PolicyEngine(),
                    approvals=_APPROVALS, approved_approval_id=aid,
                ),
                timeout,
            )
        except ToolTimeout:
            # 线程无法强杀：本次放弃，effect 标记 failed + 「可能已产生副作用」，
            # 审批写固定文案的终态，心跳在 finally 停止。
            logger.warning("approved action timed out after %ss", timeout)
            _TOOL_EFFECTS.fail(tenant, aid, tool, args, maybe_side_effect=True)
            _mark_approval_failed(aid, token, "tool timeout")
            raise HTTPException(status_code=504, detail="approved action timed out") from None
        except Exception:  # noqa: BLE001 - 执行异常只进日志，不向调用方回显
            logger.exception("approved action failed")
            # 异常发生在工具调用前后无法判定，保守标记「可能已产生副作用」。
            _TOOL_EFFECTS.fail(tenant, aid, tool, args, maybe_side_effect=True)
            _mark_approval_failed(aid, token, "execution failed")
            raise HTTPException(status_code=502, detail="approved action failed to execute") from None
        finally:
            # 工具返回或抛异常后必须停掉续期循环，避免线程泄漏。
            if heartbeat is not None:
                heartbeat.stop()

        if heartbeat is not None and heartbeat.lost:
            # 租约在执行期间被回收：不再写 done/failed，避免覆盖他人的 claim。
            logger.warning("approval %s lease was lost during execution", aid)
            raise HTTPException(status_code=409, detail="approval lease lost during execution") from None

    # 若放行失败，guarded_executor 会写入一个新的 approval_id；此时不动原审批单。
    if state.context.get("approval_id") != aid:
        if not cached:
            _TOOL_EFFECTS.fail(tenant, aid, tool, args, maybe_side_effect=False)
        _mark_approval_failed(aid, token, "approval does not match pending action")
        raise HTTPException(status_code=409, detail="approval does not match pending action")

    result = str(obs.get("result", ""))
    outcome = obs.get("outcome")
    if not cached and outcome != "success":
        # 只有工具未执行才能确认没有副作用；error/超时/异常统一视为结果不明确。
        uncertain = outcome == "error"
        _TOOL_EFFECTS.fail(tenant, aid, tool, args, maybe_side_effect=uncertain)
        if uncertain:
            _mark_approval_failed(aid, token, result)
        else:
            try:
                _APPROVALS.release(aid, token)
            except Exception:  # noqa: BLE001 - 释放失败不掩盖原始执行失败
                logger.exception("failed to release approval %s after clean tool failure", aid)
        raise HTTPException(status_code=502, detail="approved action failed to execute")

    if not cached:
        # 只存结果摘要引用（sha256），不落工具返回明文。
        _TOOL_EFFECTS.succeed(tenant, aid, tool, args, result_ref(result))

    # 两阶段第二步：成功才置 done + consumed。条件更新 0 行（含 claim_token 不匹配）
    # 说明租约已被回收/换主，此时不覆盖他人的 claim。
    try:
        _APPROVALS.complete(aid, token)
    except ValueError:
        logger.warning("approval %s could not be completed; lease lost", aid)
        _metrics.inc_approval_lease_lost()
        raise HTTPException(status_code=409, detail="approval lease lost during execution") from None
    state.observations.append(obs)
    state.messages.append({"role": "observation", "content": str(obs)})
    for key in ("pause_reason", "approval_id", "approval_tool", "approval_args"):
        state.context.pop(key, None)

    if at is not None and at.status == "paused":
        at.transition("running", "resume_approved")
        at.transition("done", "approval_executed")
    state.status = "done"
    state.answer = (
        f"approval {aid} approved by {item.get('approver', 'unknown')}; "
        f"{'replayed cached result for' if cached else 'executed'} {tool}"
    )
    _PAUSED_TASKS.delete(tenant, task_id)

    return {
        "reply": state.answer,
        "status": "done",
        "user_id": user.get("user_id", "unknown"),
        "task_id": task_id,
        "trace_id": trace_id,
        "token_usage": dict(at.token_usage) if at else None,
        "cost": at.cost if at else 0.0,
        "approval_id": aid,
        "approval_tool": tool,
        "approval_args": args,
        "executed": not cached,
        "cached": cached,
        "execution_result": obs.get("result", ""),
    }
