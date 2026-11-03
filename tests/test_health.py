from fastapi.testclient import TestClient
from apps.api.main import app

client = TestClient(app)


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
