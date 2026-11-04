"""Stage 1b Gateway - /chat routes into Agent Runtime."""
import logging
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from agent.runtime import loop as _runtime_loop
from agent.runtime.state import AgentState
from agent.runtime.llm_planner import build_deepseek_planner, get_default_client
from agent.tools.executor import (
    ToolTimeout,
    execute_with_timeout,
    guarded_executor,
    tool_timeout_seconds,
)
from infrastructure.pg.connector import get_agent_connector, get_connector
from infrastructure.pg.schema import ensure_schema
from observability.tracing import Tracer
from observability import metrics as _metrics
from observability.otel import build_otel_adapter
from integrations.scm.client import scm_trace_id
from security.approval import (
    ApprovalConflict,
    approval_lease_seconds,
    build_approval_store,
    reclaim_on_resume_enabled,
    start_lease_heartbeat,
    validate_heartbeat_settings,
)
from security.tool_effect import (
    CACHED as TOOL_EFFECT_CACHED,
    CONFLICT as TOOL_EFFECT_CONFLICT,
    build_tool_effect_store,
    result_ref,
)
from security.policy import PolicyEngine
from agent.runtime.paused_tasks import build_paused_task_store

logger = logging.getLogger(__name__)

app = FastAPI(title="Enterprise Agent Gateway", version="0.5.1")

# HITL 状态：审批单与挂起任务用 repository 抽象。
# AGENT_PG_DSN 是唯一开关；有值走 Postgres，否则自动回落 InMemory，
# 测试与本地开发不需要数据库。挂起任务按 (tenant_id, task_id) 隔离。
_APPROVALS = build_approval_store()
_PAUSED_TASKS = build_paused_task_store()
_TOOL_EFFECTS = build_tool_effect_store()


class ChatRequest(BaseModel):
    message: str
    user_id: str = "u1"
    tenant_id: str = "t1"
    task_id: str | None = None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str = "unknown"
    tenant_id: str = "t1"


class ResumeRequest(BaseModel):
    tenant_id: str = "t1"


@app.get("/health")
def health():
    return {"status": "ok", "stage": 1}


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus 文本指标；无 prometheus-client 时用内存计数器（同样可读）。"""
    try:
        payload, content_type = _metrics.render_metrics()
    except _metrics.MetricsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return Response(content=payload, media_type=content_type)


@app.on_event("startup")
def _startup():
    # 余量约束不满足直接启动失败：租约保护失效属于正确性问题，不接受静默降级。
    validate_heartbeat_settings()
    try:
        ensure_schema(get_agent_connector() or get_connector())
    except Exception as e:
        import warnings
        warnings.warn(f"memory schema init failed: {e}")
    try:
        Tracer.otel = build_otel_adapter()
    except Exception as e:  # noqa: BLE001 - 可观测性不能阻断启动
        import warnings
        warnings.warn(f"OTel adapter init failed: {e}")


def _mark_approval_failed(approval_id: str, claim_token: str, summary: str) -> None:
    """工具/执行失败时把审批置为 failed；摘要已脱敏，异常全文只进日志。"""
    try:
        _APPROVALS.fail(approval_id, claim_token, summary)
    except Exception:  # noqa: BLE001 - 标记失败不能掩盖原始错误
        logger.exception("failed to mark approval %s as failed", approval_id)


def _chat_payload(final: AgentState | None, user_id: str, trace_id: str) -> dict:
    at = final.context.get("agent_task") if final else None
    payload = {
        "reply": final.answer if final else "",
        "status": final.status if final else "failed",
        "user_id": user_id,
        "task_id": at.task_id if at else None,
        "trace_id": trace_id,
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
def chat(req: ChatRequest):
    # Stage 1b: Gateway → Runtime. 有Key走DeepSeek，无Key走echo降级。
    client, model = get_default_client()
    planner = build_deepseek_planner(client=client, model=model)
    st = AgentState(task=req.message)
    if req.task_id:
        st.task_id = req.task_id
    st.context["tenant_id"] = req.tenant_id

    # v2 Phase 3: HITL user 上下文（guarded_executor 需要 user 做 Policy 检查）
    user = {"tenant_id": req.tenant_id, "role": "admin", "user_id": req.user_id}
    st.context["user"] = user

    # v2 Phase 3: 开 trace + 把 trace_id 注入 contextvar 让 scm 客户端能埋点
    tracer = Tracer()
    tid = tracer.start_trace(req.message)
    st.context["trace_id"] = tid
    cv_token = scm_trace_id.set(tid)
    final = None
    try:
        final = _runtime_loop.run(
            st, planner=planner,
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

    at = final.context.get("agent_task") if final else None
    if at and final and final.status == "paused":
        _PAUSED_TASKS.save(req.tenant_id, at.task_id, final)

    return _chat_payload(final, req.user_id, tid)


@app.post("/approval/{approval_id}/decide")
def decide_approval(approval_id: str, req: ApprovalDecision):
    """人工审批：approve / reject。只记录决定，不触发执行。"""
    try:
        item = _APPROVALS.get(approval_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found")

    if item["user"].get("tenant_id") != req.tenant_id:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    if item["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval already {item['status']}")

    if req.decision == "approve":
        try:
            item = _APPROVALS.approve(approval_id, req.approver)
        except ApprovalConflict:
            raise HTTPException(status_code=409, detail="approval changed concurrently")
    else:
        try:
            item = _APPROVALS.reject(approval_id, req.approver)
        except ApprovalConflict:
            raise HTTPException(status_code=409, detail="approval changed concurrently")

    return {
        "approval_id": approval_id,
        "status": item["status"],
        "tool": item["tool"],
        "args": item["args"],
        "approver": item.get("approver"),
    }


@app.post("/chat/{task_id}/resume")
def resume_chat(task_id: str, req: ResumeRequest | None = None):
    """从 paused 恢复：审批通过则执行被挂起的动作，驳回则取消任务。"""
    tenant = req.tenant_id if req else "t1"
    # 惰性回收：resume 是审批单的唯一消费方，无需额外后台任务；一次条件 UPDATE
    # 把租约过期的 in_progress 退回 approved（崩溃后最多重放一次的前提）。
    if reclaim_on_resume_enabled():
        try:
            reclaimed = _APPROVALS.reclaim_expired_claims()
            if reclaimed:
                logger.warning("reclaimed %d expired approval claim(s)", reclaimed)
        except Exception:  # noqa: BLE001 - 回收失败不阻断本次 resume
            logger.exception("approval lease reclaim failed")
    state = _PAUSED_TASKS.get(tenant, task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="task not found or already completed")
    if state.status != "paused":
        raise HTTPException(status_code=409, detail="task is not paused")
    if state.context.get("tenant_id") != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")
    if state.context.get("pause_reason") != "need_approval":
        raise HTTPException(status_code=409, detail="unsupported pause reason")

    aid = state.context.get("approval_id")
    if not aid:
        raise HTTPException(status_code=409, detail="no approval attached to task")
    try:
        item = _APPROVALS.get(aid)
    except KeyError:
        raise HTTPException(status_code=404, detail="approval not found")
    if item["user"].get("tenant_id") != tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")

    user = state.context.get("user") or {"tenant_id": tenant, "role": "admin"}
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
        raise HTTPException(status_code=409, detail="approval is already claimed")
    token = claimed["claim_token"]

    tool = item["tool"]
    args = item["args"]

    # 写工具幂等：同一 approval + 工具 + 参数只真正执行一次。占位先于心跳；
    # succeeded 直接回放引用，started 说明上一次执行仍在进行/已崩溃（绝不并跑）。
    effect = _TOOL_EFFECTS.begin(tenant, aid, tool, args)
    if effect["decision"] == TOOL_EFFECT_CONFLICT:
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
        obs = {"tool": tool, "result": effect.get("result_ref") or "cached", "cached": True}
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
            raise HTTPException(status_code=504, detail="approved action timed out")
        except Exception:  # noqa: BLE001 - 执行异常只进日志，不向调用方回显
            logger.exception("approved action failed")
            # 异常发生在工具调用前后无法判定，保守标记「可能已产生副作用」。
            _TOOL_EFFECTS.fail(tenant, aid, tool, args, maybe_side_effect=True)
            _mark_approval_failed(aid, token, "execution failed")
            raise HTTPException(status_code=502, detail="approved action failed to execute")
        finally:
            # 工具返回或抛异常后必须停掉续期循环，避免线程泄漏。
            if heartbeat is not None:
                heartbeat.stop()

        if heartbeat is not None and heartbeat.lost:
            # 租约在执行期间被回收：不再写 done/failed，避免覆盖他人的 claim。
            logger.warning("approval %s lease was lost during execution", aid)
            raise HTTPException(status_code=409, detail="approval lease lost during execution")

    # 若放行失败，guarded_executor 会写入一个新的 approval_id；此时不动原审批单。
    if state.context.get("approval_id") != aid:
        if not cached:
            _TOOL_EFFECTS.fail(tenant, aid, tool, args, maybe_side_effect=False)
        _mark_approval_failed(aid, token, "approval does not match pending action")
        raise HTTPException(status_code=409, detail="approval does not match pending action")

    result = str(obs.get("result", ""))
    if not cached and (result.startswith("tool error ") or result.startswith("unknown tool:")):
        # 工具失败的 Observation 已只含工具名（无异常文本/栈），直接作为脱敏摘要。
        # tool error 无法判定是否部分生效，保守标记可能已产生副作用；unknown tool 则没有。
        _TOOL_EFFECTS.fail(tenant, aid, tool, args,
                           maybe_side_effect=result.startswith("tool error "))
        _mark_approval_failed(aid, token, result)
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
        raise HTTPException(status_code=409, detail="approval lease lost during execution")
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
