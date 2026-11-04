"""Test-only ASGI app for kill/restart drills.

Run as a *real* uvicorn process (``uvicorn drill_app:app --app-dir tests``). The
production gateway is imported unchanged; only two seams are swapped so the
drill is deterministic and observable from outside the process:

1. the planner is replaced by one that always issues a single ``call_tool`` to
   ``payment.execute`` (HIGH_RISK, so PolicyEngine forces a human approval);
2. the default tool registry gets an instrumented replacement for that tool,
   which appends to a counter file and optionally sleeps.

Env:
  AGENT_DRILL_COUNTER  append-only file, one ``invoked``/``finished`` line per call
  AGENT_DRILL_SLEEP    seconds the drilled tool sleeps before finishing
"""
import os
import time

import agent.tools.executor as executor_module
import apps.api.main as main_module
from agent.tools.builtin import build_default_registry
from agent.tools.registry import Tool

TOOL = "payment.execute"  # HIGH_RISK in security/policy.py → requires approval

COUNTER = os.environ.get("AGENT_DRILL_COUNTER", "")
SLEEP = float(os.environ.get("AGENT_DRILL_SLEEP", "0") or 0)


def _record(kind: str) -> None:
    with open(COUNTER, "a", encoding="utf-8") as fh:
        fh.write(kind + "\n")
        fh.flush()


def _drill_tool(args: dict):
    _record("invoked")
    time.sleep(float(args.get("sleep", SLEEP) or 0))
    _record("finished")
    return "drill tool done"


def _drill_planner(state):
    if state.observations:
        return {"action": "finish", "answer": "drill finished"}
    tenant = state.context.get("tenant_id") or "t1"
    return {"action": "call_tool", "tool": TOOL,
            "args": {"tenant_id": tenant, "sleep": SLEEP}}


_registry = build_default_registry()
_registry.register(Tool(TOOL, "drill tool (sleep + counter)", _drill_tool))
executor_module._default_registry = _registry

# /chat looks this name up in its module globals at call time.
main_module.build_deepseek_planner = lambda client=None, model=None: _drill_planner

app = main_module.app
