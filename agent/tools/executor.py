"""Registry Executor: 把 Planner 的 call_tool 翻译成真实调用。工具失败→Observation，不抛异常。"""
from .builtin import build_default_registry

_default_registry = None


def _registry():
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry


def registry_executor(plan: dict, state, registry=None) -> dict:
    reg = registry or _registry()
    tool = plan.get("tool", "")
    args = plan.get("args", {}) or {}
    ok = True
    try:
        result = reg.call(tool, args)
    except KeyError:
        ok = False
        result = f"unknown tool: {tool}"
    except Exception as e:  # noqa: BLE001 - 工具错误必须变成Observation
        ok = False
        result = f"tool error {tool}: {e}"
    try:
        from observability.metrics import record_tool_call
        record_tool_call(tool, ok)
    except Exception:  # noqa: BLE001
        pass
    state.tool_calls.append({"tool": tool, "args": args})
    return {"tool": tool, "result": str(result)}


def guarded_executor(plan: dict, state, registry=None, user=None,
                     policy=None, approvals=None, approved_approval_id=None) -> dict:
    """带Policy+HITL的执行器：deny/need_approval都不直调工具。

    approved_approval_id 只在恢复路径使用：当审批单已批准，且 tool+args
    与待执行动作完全一致时，放行这一次调用；否则仍按 need_approval 挂起。
    """
    tool = plan.get("tool", "")
    args = plan.get("args", {}) or {}
    if policy is not None and user is not None:
        decision = policy.check(user, tool, args).get("decision")
        if decision == "deny":
            return {"tool": tool, "result": f"Policy DENY: role={user.get('role')} tool={tool}"}
        if decision == "need_approval":
            if approved_approval_id is not None and approvals is not None:
                try:
                    item = approvals.get(approved_approval_id)
                except KeyError:
                    item = None
                if (item and item.get("status") == "approved"
                        and not item.get("consumed")
                        and item.get("tool") == tool
                        and item.get("args") == args):
                    return registry_executor(plan, state, registry=registry)
            aid = None
            if approvals is not None:
                aid = approvals.request(user, tool, args)
            ctx = getattr(state, "context", None)
            if isinstance(ctx, dict):
                ctx["pause_reason"] = "need_approval"
                ctx["approval_id"] = aid
                ctx["approval_tool"] = tool
                ctx["approval_args"] = args
            return {"tool": tool, "result": f"need approval {aid}: {tool} pending human review"}
    return registry_executor(plan, state, registry=registry)
