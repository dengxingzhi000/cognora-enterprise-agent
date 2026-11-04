from fastapi.testclient import TestClient
from apps.api.main import app

client = TestClient(app)


def _assert_no_exception_leak(body: str) -> None:
    """异常路径的响应体不得含栈、绝对路径、异常类名或异常消息。"""
    for marker in ("Traceback", "RuntimeError", "leak-marker",
                   "secret", "/opt/agent/"):
        assert marker not in body


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_chat_echo():
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 200
    assert "hi" in r.json()["reply"]


def test_unexpected_error_does_not_leak_exception_text(monkeypatch):
    """The boundary must not echo raw exception text to the caller."""
    import apps.api.main as main

    def boom(*args, **kwargs):
        raise RuntimeError("leak-marker-123")

    monkeypatch.setattr(main._runtime_loop, "run", boom)
    isolated = TestClient(main.app, raise_server_exceptions=False)
    r = isolated.post("/chat", json={"message": "x"})
    assert r.status_code == 500
    assert "leak-marker-123" not in r.text


def test_planner_error_response_is_generic(monkeypatch):
    """Planner exceptions must not reach /chat (py/stack-trace-exposure, alert #1)."""
    import apps.api.main as main

    def boom_planner(state):
        raise RuntimeError("leak-marker-planner at C:\\secret\\planner.py")

    monkeypatch.setattr(main, "build_deepseek_planner",
                        lambda client, model: boom_planner)
    isolated = TestClient(main.app, raise_server_exceptions=False)
    r = isolated.post("/chat", json={"message": "x"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["trace_id"]
    _assert_no_exception_leak(r.text)


def test_tool_error_observation_carries_no_exception_text():
    """Registry executor turns a tool exception into a marker-only Observation."""
    from types import SimpleNamespace

    from agent.tools.executor import registry_executor

    class FailingRegistry:
        def call(self, tool, args):
            raise RuntimeError("leak-marker-tool at C:\\secret\\tools.py")

    result = registry_executor(
        {"tool": "metrics.get", "args": {}},
        SimpleNamespace(tool_calls=[]),
        registry=FailingRegistry(),
    )
    assert result["result"] == "tool error metrics.get"
    _assert_no_exception_leak(result["result"])


def test_resume_tool_failure_marks_approval_failed_without_leak(monkeypatch):
    """Approved tool failure → approval failed with a sanitized summary, 502, no replay."""
    import apps.api.main as main
    import agent.tools.executor as executor_mod
    from infrastructure.pg import connector as conn_mod
    from infrastructure.pg import schema as schema_mod

    monkeypatch.setattr(conn_mod, "get_connector", lambda: None)
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)

    def fake_planner(state):
        if state.observations:
            return {"action": "finish", "answer": "done"}
        return {"action": "call_tool", "tool": "scm.purchase.create",
                "args": {"sku": "X", "tenant_id": state.context.get("tenant_id", "t1")}}

    monkeypatch.setattr(main, "build_deepseek_planner",
                        lambda client, model: fake_planner)

    class FailingRegistry:
        def call(self, tool, args):
            raise RuntimeError("leak-marker-resume at C:\\secret\\resume.py")

    monkeypatch.setattr(executor_mod, "_default_registry", FailingRegistry())

    client = TestClient(main.app)
    body = client.post("/chat", json={"message": "buy X", "tenant_id": "t1",
                                     "user_id": "u1"}).json()
    assert body["status"] == "paused"
    decided = client.post(f"/approval/{body['approval_id']}/decide",
                          json={"decision": "approve", "approver": "mgr-1",
                                "tenant_id": "t1"})
    assert decided.status_code == 200
    resumed = client.post(f"/chat/{body['task_id']}/resume", json={"tenant_id": "t1"})
    assert resumed.status_code == 502
    assert resumed.json()["detail"] == "approved action failed to execute"
    _assert_no_exception_leak(resumed.text)

    # 两阶段：失败落在 failed，只保留脱敏摘要（工具名），不再可 resume。
    item = main._APPROVALS.get(body["approval_id"])
    assert item["status"] == "failed"
    assert item["last_error"] == "tool error scm.purchase.create"
    assert "leak-marker" not in item["last_error"]
    assert "RuntimeError" not in item["last_error"]

    again = client.post(f"/chat/{body['task_id']}/resume", json={"tenant_id": "t1"})
    assert again.status_code == 409
