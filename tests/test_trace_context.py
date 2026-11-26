from integrations.scm.client import ScmClient
from observability.trace_context import bind_traceparent, new_traceparent, reset_traceparent


def test_scm_client_propagates_w3c_traceparent(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        class Response:
            status_code = 200
            text = "{}"
            def json(self):
                return {"code": 200, "data": {}}
        return Response()

    monkeypatch.setattr("httpx.request", fake_request)
    token = bind_traceparent(new_traceparent())
    try:
        ScmClient(gateway_url="http://gateway").get("/test")
    finally:
        reset_traceparent(token)
    assert captured["headers"]["traceparent"].startswith("00-")
    assert len(captured["headers"]["traceparent"]) == 55
