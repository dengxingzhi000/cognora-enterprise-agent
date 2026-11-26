"""LangGraph runtime: Task → Planner → Executor → Observation → Final."""
import dataclasses
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from langgraph.graph import END, StateGraph

from agent.tools.executor import ToolTimeout, execute_with_timeout
from observability.tracing import Tracer

from .agent_task import AgentTask
from .counter import get_counter, get_pricing
from .planner import default_executor, default_planner
from .state import AgentState

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent.context.assembler import ContextAssembler


class RuntimeGraphState(TypedDict):
    state: AgentState


def _wrap(state_or_task) -> AgentTask:
    if isinstance(state_or_task, AgentTask):
        return state_or_task
    return AgentTask.from_state(state_or_task)


def _emit_status(task: AgentTask, reason: str) -> None:
    # 情景记忆：每次状态迁移都落一条 memory_episodic（失败不阻断状态机）。
    try:
        from agent.memory.episodic import record_task_transition
        record_task_transition(task, reason)
    except Exception:
        pass
    trace_id = task.state.context.get("trace_id")
    if not trace_id:
        return
    tracer = Tracer._shared or Tracer()
    try:
        data = {"task_id": task.task_id,
                "from": task.history[-1]["from"] if task.history else None,
                "to": task.status, "reason": reason}
        tracer.log_event(trace_id, "task_status_changed", data)
        from observability.metrics import inc_task_status
        inc_task_status(task.status)
    except Exception:
        pass


def _record_tokens(task: AgentTask, messages: list[dict], output_text: str = "") -> None:
    try:
        c = get_counter()
        counting = [{"role": "user", "content": task.state.task or ""}]
        for m in (messages or []):
            if not (isinstance(m, dict) and m.get("content") == task.state.task):
                counting.append(m)
        usage = c.count_messages(counting)
        if usage["input"]:
            task.record_tokens(usage["input"], 0)
            p = get_pricing("deepseek-chat")
            task.cost += (usage["input"] / 1000) * p.input_per_1k
        if output_text:
            out = c.count_messages([{"role": "assistant", "content": output_text}])
            if out["input"]:
                task.record_tokens(0, out["input"])
                p = get_pricing("deepseek-chat")
                task.cost += (out["input"] / 1000) * p.output_per_1k
    except Exception:
        pass


def _fail(task: AgentTask, inner: AgentState, answer: str, reason: str,
          messages: list[dict], failure_code: str | None = None) -> None:
    inner.status = "failed"
    inner.answer = answer
    if failure_code:
        inner.context["failure_code"] = failure_code
    _record_tokens(task, messages, "")
    task.transition("failed", reason)
    _emit_status(task, reason)


def _planner_input(inner: AgentState, assembler: "ContextAssembler | None",
                   previous_messages: list[dict]):
    if assembler is None:
        return inner, inner.messages
    try:
        llm_messages, report = assembler.assemble(
            {"task": inner.task, "messages": inner.messages,
             "observations": inner.observations, "tool_calls": inner.tool_calls,
             "rag": inner.context.get("rag", []),
             "system": inner.context.get("system", ""),
             "tenant_id": inner.context.get("tenant_id", "default")})
        inner.context["context_report"] = report
        trace_id = inner.context.get("trace_id")
        if trace_id:
            try:
                tracer = Tracer._shared or Tracer()
                tracer.log_event(trace_id, "context_assemble", report)
            except Exception:
                pass
        planner_state = dataclasses.replace(
            inner,
            messages=[{"role": m.get("role", "user"), "content": m.get("content", "")}
                      for m in llm_messages],
            observations=list(inner.observations),
            tool_calls=list(inner.tool_calls),
            context=dict(inner.context))
        return planner_state, llm_messages
    except Exception:
        return inner, previous_messages


def build_runtime_graph(
    task: AgentTask,
    planner: Callable = default_planner,
    executor: Callable = default_executor,
    max_iterations: int = 8,
    assembler: "ContextAssembler | None" = None,
):
    inner = task.state
    llm_messages = inner.messages

    def plan_node(graph_state: RuntimeGraphState):
        nonlocal llm_messages
        current = graph_state["state"]
        planner_state, llm_messages = _planner_input(current, assembler, llm_messages)
        try:
            plan = planner(planner_state)
        except Exception:  # noqa: BLE001 - 异常只进日志，不回显到响应
            logger.exception("planner failed")
            current.plan = None
            _fail(task, current, "planner_error", "planner_error", llm_messages)
            return {"state": current}

        current.plan = plan
        action = plan.get("action")
        if action == "finish":
            current.answer = plan.get("answer", "")
            current.status = "done"
            _record_tokens(task, llm_messages, current.answer)
            task.transition("done", "finish_received")
            _emit_status(task, "finish_received")
        elif action == "call_tool":
            _record_tokens(task, llm_messages, "")
        else:
            _fail(task, current, f"unknown action: {action}", "unknown_action", llm_messages)
        return {"state": current}

    def execute_node(graph_state: RuntimeGraphState):
        current = graph_state["state"]
        plan = current.plan
        assert plan is not None
        try:
            obs = execute_with_timeout(lambda: executor(plan, current))
        except ToolTimeout:
            logger.warning("tool %s timed out", plan.get("tool", ""))
            _fail(task, current, "tool_timeout", "tool_timeout", llm_messages,
                  failure_code="tool_timeout")
            return {"state": current}
        except Exception:  # noqa: BLE001 - 异常只进日志，不回显到响应
            logger.exception("executor failed")
            _fail(task, current, "executor_error", "executor_error", llm_messages)
            return {"state": current}

        current.observations.append(obs)
        current.messages.append({"role": "observation", "content": str(obs)})
        if obs.get("outcome") == "approval_required":
            current.status = "paused"
            task.transition("paused", "need_approval")
            _emit_status(task, "need_approval")
        else:
            current.iteration += 1
        return {"state": current}

    def after_plan(graph_state: RuntimeGraphState):
        current = graph_state["state"]
        if current.status != "running":
            return END
        assert current.plan is not None
        action = current.plan.get("action")
        return "execute" if action == "call_tool" else END

    def after_execute(graph_state: RuntimeGraphState):
        current = graph_state["state"]
        if current.status != "running":
            return END
        if current.iteration >= max_iterations:
            _fail(task, current, "max_iterations exceeded", "max_iterations", llm_messages)
            return END
        return "plan"

    graph: StateGraph[RuntimeGraphState] = StateGraph(RuntimeGraphState)
    graph.add_node("plan", plan_node)  # type: ignore[arg-type]
    graph.add_node("execute", execute_node)  # type: ignore[arg-type]
    graph.set_entry_point("plan")
    graph.add_conditional_edges("plan", after_plan, {"execute": "execute", END: END})
    graph.add_conditional_edges("execute", after_execute, {"plan": "plan", END: END})
    return graph.compile()


def run(
    state: AgentState,
    planner: Callable = default_planner,
    executor: Callable = default_executor,
    max_iterations: int = 8,
    assembler: "ContextAssembler | None" = None,
) -> AgentState:
    task = _wrap(state)
    inner = task.state if isinstance(state, AgentTask) else state
    task.transition("running", "loop_start")
    _emit_status(task, "loop_start")
    inner.status = "running"
    try:
        if inner.iteration >= max_iterations:
            _fail(task, inner, "max_iterations exceeded", "max_iterations", inner.messages)
        else:
            graph = build_runtime_graph(
                task, planner=planner, executor=executor,
                max_iterations=max_iterations, assembler=assembler,
            )
            result = graph.invoke(
                {"state": inner},
                config={"recursion_limit": max(25, max_iterations * 2 + 5)},
            )
            inner = result["state"]
    finally:
        inner.context["agent_task"] = task
    return inner
