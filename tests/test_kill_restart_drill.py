"""Kill/restart drills against a real uvicorn process and a real Postgres.

Each case boots ``uvicorn drill_app:app`` as a subprocess on a random free port,
drives ``/chat`` so a high-risk tool is paused for approval, sends ``SIGKILL``
to the whole process group mid-flight, restarts a brand-new process, and then
asserts what survived in ``agent_approval`` / ``agent_paused_task``.

Only runs when ``AGENT_TEST_PG_DSN`` is set. CI's one-shot ``postgres:16``
service provides it, and the "Drill cases must run, not skip" workflow step
fails the job if any of these skip, so they are never silently green.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

DSN = os.environ.get("AGENT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="AGENT_TEST_PG_DSN not set; real uvicorn kill/restart drill skipped"
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = "payment.execute"
START_TIMEOUT = 40.0


# --- process plumbing -------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornProc:
    """A real ``uvicorn`` subprocess we can SIGKILL and start again."""

    def __init__(self, counter_path: str, sleep_seconds: float):
        self.counter = counter_path
        self.sleep = sleep_seconds
        self.proc = None
        self.port = None

    def start(self) -> "UvicornProc":
        port = _free_port()
        env = dict(os.environ)
        env["AGENT_PG_DSN"] = DSN
        env["AGENT_DRILL_COUNTER"] = self.counter
        env["AGENT_DRILL_SLEEP"] = str(self.sleep)
        env.pop("AGENT_PG_OFFLINE", None)
        env.pop("PG_OFFLINE", None)
        cmd = [
            sys.executable, "-m", "uvicorn", "drill_app:app",
            "--app-dir", "tests", "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ]
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        self.proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, **kwargs)
        self.port = port
        self._wait_ready()
        return self

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _wait_ready(self) -> None:
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"uvicorn exited early rc={self.proc.returncode}")
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.25)
        raise AssertionError("uvicorn did not become healthy in time")

    def kill9(self) -> None:
        """Hard-kill the whole process group (POSIX) or the process (Windows)."""
        if self.proc is None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            else:
                self.proc.kill()
        finally:
            try:
                self.proc.wait(timeout=15)
            finally:
                self.proc = None

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - already dead
            try:
                self.proc.kill()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self.proc = None


# --- HTTP + counter helpers -------------------------------------------------

def _post(url: str, payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _counter_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def _wait_for_line(path: str, line: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if line in _counter_lines(path):
            return True
        time.sleep(0.05)
    return False


# --- DB helpers -------------------------------------------------------------

@pytest.fixture(scope="module")
def db():
    from infrastructure.pg.connector import PGConnector
    from infrastructure.pg.schema import ensure_schema

    c = PGConnector(DSN, pool_size=2)
    assert c.healthcheck()
    ensure_schema(c)
    return c


def _tenant() -> str:
    return f"drill-{uuid.uuid4().hex[:8]}"


def _paused_row(db, tenant: str, task_id: str):
    return db.fetch_one(
        "SELECT state FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
        (tenant, task_id),
    )


def _approval_row(db, aid: str):
    return db.fetch_one(
        "SELECT status, consumed FROM agent_approval WHERE id = ?", (aid,)
    )


def _cleanup(db, tenant: str, task_id: str | None = None, aid: str | None = None) -> None:
    try:
        if aid:
            db.execute("DELETE FROM agent_approval WHERE id = ?", (aid,))
        if task_id:
            db.execute(
                "DELETE FROM agent_paused_task WHERE tenant_id = ? AND task_id = ?",
                (tenant, task_id),
            )
        db.execute("DELETE FROM memory_episodic WHERE tenant_id = ?", (tenant,))
    except Exception:  # noqa: BLE001 - cleanup must not mask the real assertion
        pass


def _pause_for_approval(app: UvicornProc, tenant: str) -> tuple[str, str]:
    status, body = _post(app.base + "/chat",
                         {"message": "pay an invoice",
                          "tenant_id": tenant, "user_id": "u1"})
    assert status == 200, body
    assert body["status"] == "paused", body
    assert body["approval_id"], body
    assert body["approval_tool"] == TOOL, body
    return body["task_id"], body["approval_id"]


def _approve(app: UvicornProc, aid: str, tenant: str):
    return _post(app.base + f"/approval/{aid}/decide",
                 {"decision": "approve", "approver": "mgr-1", "tenant_id": tenant})


# --- scenario 1: kill while WAITING_APPROVAL --------------------------------

def test_kill_while_waiting_approval_then_resume(db, tmp_path):
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 0).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)

        # The paused row must be durable *before* the crash.
        assert _paused_row(db, tenant, task_id) is not None

        app.kill9()

        # --- restart ---
        app = UvicornProc(counter, 0).start()

        # Row survived the hard kill (no 404, no lost row).
        assert _paused_row(db, tenant, task_id) is not None

        # Resume reaches the row: 409 (approval still pending), never 404.
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 409, body
        assert "not approved" in body.get("detail", ""), body

        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 200, body
        assert body["status"] == "done" and body["executed"] is True, body

        # The approved action ran exactly once after recovery.
        lines = _counter_lines(counter)
        assert lines.count("invoked") == 1, lines
        assert lines.count("finished") == 1, lines

        assert _paused_row(db, tenant, task_id) is None
        assert _approval_row(db, aid)["consumed"] is True
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- scenario 2: kill while the tool is executing ---------------------------

def test_kill_during_tool_execution_is_retryable_not_consumed_without_result(db, tmp_path):
    """The approval must not be left consumed without a result.

    Current implementation executes the tool first and consumes the approval
    afterwards, so a kill mid-tool leaves ``approved`` + ``consumed=FALSE``:
    retryable, but the side effect is replayed (asserted below and reported as
    a known gap). It is *not* left in the "consumed but no result" state.
    """
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 5.0).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        resume_errors: list[Exception] = []

        def _resume():
            try:
                _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
            except Exception as e:  # noqa: BLE001 - the process dies under us
                resume_errors.append(e)

        worker = threading.Thread(target=_resume, daemon=True)
        worker.start()
        assert _wait_for_line(counter, "invoked", timeout=10), "tool never started"
        app.kill9()
        worker.join(timeout=10)
        assert _counter_lines(counter).count("finished") == 0

        # --- restart ---
        app = UvicornProc(counter, 5.0).start()

        row = _approval_row(db, aid)
        assert row["status"] == "approved", row
        assert row["consumed"] is False, f"bad state: consumed without result {row}"
        assert _paused_row(db, tenant, task_id) is not None

        # Retry completes the approved action; the counter shows a replay.
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 200, body
        assert body["status"] == "done" and body["executed"] is True, body

        lines = _counter_lines(counter)
        assert lines.count("invoked") == 2, lines  # killed attempt + retry
        assert lines.count("finished") == 1, lines
        assert _approval_row(db, aid)["consumed"] is True
        assert _paused_row(db, tenant, task_id) is None
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- scenario 3: kill after approval, before resume ------------------------

def test_kill_after_approval_resume_runs_once(db, tmp_path):
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 0).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        app.kill9()

        # --- restart: the approved decision is durable ---
        app = UvicornProc(counter, 0).start()
        row = _approval_row(db, aid)
        assert row["status"] == "approved" and row["consumed"] is False, row

        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 200, body
        assert body["status"] == "done" and body["executed"] is True, body
        assert body["approval_id"] == aid, body

        lines = _counter_lines(counter)
        assert lines.count("invoked") == 1, lines
        assert lines.count("finished") == 1, lines

        # Consumed + paused row deleted → a second resume is a clean 404 and
        # must not replay the side effect.
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 404, body
        assert _counter_lines(counter).count("invoked") == 1
        assert _approval_row(db, aid)["consumed"] is True
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)
