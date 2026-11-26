from types import SimpleNamespace

import pytest

from agent.runtime.agent_task import AgentTask
from agent.runtime.loop import run
from agent.runtime.state import AgentState


def _planner(state):
    if state.iteration == 0:
        return {"action": "call_tool", "tool": "metrics.get", "args": {}}
    return {"action": "finish", "answer": "graph done"}


def _executor(plan, state):
    return {"tool": plan["tool"], "result": "ok", "outcome": "success"}


def test_loop_delegates_to_real_langgraph(monkeypatch):
    import agent.runtime.loop as loop_module

    calls = []
    original = loop_module.build_runtime_graph

    def recording_builder(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(loop_module, "build_runtime_graph", recording_builder)
    final = run(AgentState(task="check"), planner=_planner, executor=_executor)

    assert final.status == "done"
    assert final.answer == "graph done"
    assert len(calls) == 1


def test_runtime_graph_exposes_plan_execute_cycle():
    from agent.runtime.loop import build_runtime_graph

    state = AgentState(task="check")
    task = AgentTask.from_state(state)
    task.transition("running", "loop_start")
    state.status = "running"
    graph = build_runtime_graph(task, planner=_planner, executor=_executor, max_iterations=3)
    nodes = set(graph.get_graph().nodes)

    assert {"plan", "execute"} <= nodes
    result = graph.invoke({"state": state}, config={"recursion_limit": 20})
    assert result["state"].status == "done"
    assert result["state"].iteration == 1


def test_graph_preserves_iteration_limit():
    calls = []

    def planner(state):
        return {"action": "call_tool", "tool": "metrics.get", "args": {}}

    def executor(plan, state):
        calls.append(plan["tool"])
        return {"tool": plan["tool"], "result": "ok", "outcome": "success"}

    final = run(AgentState(task="loop"), planner=planner, executor=executor, max_iterations=2)
    assert final.status == "failed"
    assert final.answer == "max_iterations exceeded"
    assert final.iteration == 2
    assert len(calls) == 2


@pytest.mark.parametrize("max_iterations", [0, -1])
def test_graph_rejects_nonpositive_iteration_budget(max_iterations):
    calls = []

    def planner(state):
        calls.append("planner")
        return {"action": "finish", "answer": "unreachable"}

    final = run(AgentState(task="loop"), planner=planner, max_iterations=max_iterations)
    assert final.status == "failed"
    assert final.answer == "max_iterations exceeded"
    assert calls == []


def test_graph_rejects_pre_exhausted_state():
    calls = []
    state = AgentState(task="loop", iteration=8)

    def planner(current):
        calls.append("planner")
        return {"action": "finish", "answer": "unreachable"}

    final = run(state, planner=planner, max_iterations=8)
    assert final.status == "failed"
    assert final.answer == "max_iterations exceeded"
    assert calls == []


def test_graph_preserves_token_and_cost_accounting(monkeypatch):
    import agent.runtime.loop as loop_module

    class Counter:
        def count_messages(self, messages):
            return {"input": len(messages)}

    monkeypatch.setattr(loop_module, "get_counter", lambda: Counter())
    monkeypatch.setattr(
        loop_module, "get_pricing",
        lambda model: SimpleNamespace(input_per_1k=0.001, output_per_1k=0.002),
    )
    state = AgentState(task="check")
    final = run(state, planner=_planner, executor=_executor)
    task = final.context["agent_task"]

    assert task.token_usage == {"input": 3, "output": 1, "total": 4}
    assert task.cost == pytest.approx(0.000005)


def test_graph_falls_back_to_unassembled_state_on_assembler_error():
    seen = []

    class BrokenAssembler:
        def assemble(self, payload):
            raise RuntimeError("assembler unavailable")

    def planner(state):
        seen.append(state)
        return {"action": "finish", "answer": "ok"}

    state = AgentState(task="check")
    final = run(state, planner=planner, assembler=BrokenAssembler())
    assert final.status == "done"
    assert seen == [state]
    assert "context_report" not in final.context
