"""Kill/restart + two-phase resume drills against a real uvicorn and real Postgres.

Each case boots ``uvicorn drill_app:app`` as a subprocess on a random free port,
drives ``/chat`` so a high-risk tool is paused for approval, then exercises the
claim → execute → done/failed state machine, including ``SIGKILL`` of the whole
process group and a brand-new process.

Only runs when ``AGENT_TEST_PG_DSN`` is set. CI's one-shot ``postgres:16``
service provides it, and the "Concurrency and kill/restart drills must run,
not skip" step fails the job if any of these skip.
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
from concurrent.futures import ThreadPoolExecutor

import pytest

DSN = os.environ.get("AGENT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="AGENT_TEST_PG_DSN not set; real uvicorn kill/restart drill skipped"
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = "payment.execute"
START_TIMEOUT = 40.0
LEASE_SECONDS = 2  # 测试用短租约；语义与默认 300s 相同


# --- process plumbing -------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornProc:
    """A real ``uvicorn`` subprocess we can SIGKILL and start again."""

    def __init__(self, counter_path: str, sleep_seconds: float, *, fail: bool = False,
                 lease_seconds: int = LEASE_SECONDS, heartbeat: bool = True,
                 timeout_seconds: float | None = None):
        self.counter = counter_path
        self.sleep = sleep_seconds
        self.fail = fail
        self.lease_seconds = lease_seconds
        self.heartbeat = heartbeat
        self.timeout_seconds = timeout_seconds
        self.proc = None
        self.port = None

    def start(self) -> "UvicornProc":
        port = _free_port()
        env = dict(os.environ)
        env["AGENT_PG_DSN"] = DSN
        env["AGENT_DRILL_COUNTER"] = self.counter
        env["AGENT_DRILL_SLEEP"] = str(self.sleep)
        env["AGENT_DRILL_FAIL"] = "1" if self.fail else "0"
        env["AGENT_APPROVAL_LEASE_SECONDS"] = str(self.lease_seconds)
        env["AGENT_APPROVAL_HEARTBEAT_ENABLED"] = "1" if self.heartbeat else "0"
        if self.timeout_seconds is not None:
            env["AGENT_TOOL_TIMEOUT_SECONDS"] = str(self.timeout_seconds)
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

def _post(url: str, payload: dict, timeout: float = 60):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        "SELECT status, consumed, last_error, "
        "(lease_until IS NOT NULL AND lease_until > now()) AS lease_live "
        "FROM agent_approval WHERE id = ?", (aid,)
    )


def _repo(db):
    """测试进程侧用一个仓储实例调用 reclaim（与 uvicorn 进程走同一张表）。"""
    from security.approval import ApprovalStore, PostgresApprovalRepository

    return ApprovalStore(PostgresApprovalRepository(db))


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


# --- case 1: two concurrent resumes, exactly one executes -------------------

def test_concurrent_resume_only_one_executes(db, tmp_path):
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 3.0).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        barrier = threading.Barrier(2)
        results: list[int] = []

        def _resume():
            barrier.wait()
            status, body = _post(app.base + f"/chat/{task_id}/resume",
                                 {"tenant_id": tenant})
            results.append(status)

        with ThreadPoolExecutor(max_workers=2) as pool:
            for _ in range(2):
                pool.submit(_resume)

        # claim is atomic: exactly one 200, the other 409.
        assert sorted(results) == [200, 409], results
        lines = _counter_lines(counter)
        assert lines.count("invoked") == 1, lines
        assert lines.count("finished") == 1, lines

        row = _approval_row(db, aid)
        assert row["status"] == "done" and row["consumed"] is True, row
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 404, body
        assert _counter_lines(counter).count("invoked") == 1
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- case 2: kill mid-tool; live lease blocks retry, expiry reclaims once ---

def test_kill_mid_tool_lease_blocks_retry_then_reclaims_once(db, tmp_path):
    """A crash mid-tool leaves ``in_progress`` with a live lease.

    While the lease is live a retry is rejected (409). After the lease expires
    (fast-forwarded here to keep the drill fast/deterministic) the lazy reclaim
    makes exactly **one** replay possible — asserted via the counter.
    """
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 3.0).start()
    standby = None
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        # Pre-warm a second process so the post-kill retry needs no restart wait
        # (keeps the 2 s lease window deterministic). It is a standby, not a
        # multi-instance ownership test.
        standby = UvicornProc(counter, 3.0).start()

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

        row = _approval_row(db, aid)
        assert row["status"] == "in_progress", row
        assert row["consumed"] is False, row
        assert row["lease_live"] is True, row  # 租约未过期

        status, body = _post(standby.base + f"/chat/{task_id}/resume",
                             {"tenant_id": tenant})
        assert status == 409, body  # claim held by the dead process until lease expiry

        # 等价于等待租约过期（测试里不真的睡 300s）。
        db.execute("UPDATE agent_approval SET lease_until = now() - INTERVAL '1 second' "
                   "WHERE id = ?", (aid,))

        status, body = _post(standby.base + f"/chat/{task_id}/resume",
                             {"tenant_id": tenant})
        assert status == 200, body
        assert body["status"] == "done" and body["executed"] is True, body

        # 原始执行 + 至多 1 次重放（重放次数 ≤ 1，副作用非幂等）。
        lines = _counter_lines(counter)
        assert lines.count("invoked") == 2, lines
        assert lines.count("finished") == 1, lines
        assert _approval_row(db, aid)["consumed"] is True
        assert _paused_row(db, tenant, task_id) is None
    finally:
        app.stop()
        if standby is not None:
            standby.stop()
        _cleanup(db, tenant, task_id, aid)


# --- case 3: tool raises → failed, sanitized summary, no resume -------------

def test_tool_failure_marks_failed_and_blocks_resume(db, tmp_path):
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 0, fail=True).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 502, body
        assert body["detail"] == "approved action failed to execute", body
        assert "boom" not in json.dumps(body), body

        row = db.fetch_one(
            "SELECT status, last_error FROM agent_approval WHERE id = ?", (aid,))
        assert row["status"] == "failed", row
        err = (row["last_error"] or "")
        assert err.startswith("tool error "), err
        assert "Traceback" not in err and "RuntimeError" not in err, err
        assert "boom" not in err and "/" not in err and "\\" not in err, err

        lines = _counter_lines(counter)
        assert lines.count("invoked") == 1, lines
        assert lines.count("finished") == 0, lines

        # failed is terminal for resume; the paused row is kept for inspection.
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 409, body
        assert _counter_lines(counter).count("invoked") == 1
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- case 4a: kill while WAITING_APPROVAL ------------------------------------

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

        lines = _counter_lines(counter)
        assert lines.count("invoked") == 1, lines
        assert lines.count("finished") == 1, lines

        assert _paused_row(db, tenant, task_id) is None
        assert _approval_row(db, aid)["consumed"] is True
        assert _approval_row(db, aid)["status"] == "done"
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- case 4b: kill after approval, before resume ---------------------------

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


# --- case 5: long tool run must not fall into the reclaim window ------------

def test_long_tool_run_survives_reclaim_window(db, tmp_path):
    """工具运行 5s > 2s 租约：心跳续期期间外部 reclaim 必须回收 0 条。

    这是 v0.5.5「租约必须大于最大工具时长」这一脆弱假设的反证：执行期心跳
    一旦真的在跑，长任务就不会在运行中被误回收。
    """
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 5.0, lease_seconds=2).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        results: list[tuple[int, dict]] = []

        def _resume():
            results.append(_post(app.base + f"/chat/{task_id}/resume",
                                 {"tenant_id": tenant}))

        worker = threading.Thread(target=_resume, daemon=True)
        worker.start()
        assert _wait_for_line(counter, "invoked", timeout=10), "tool never started"

        # 超过一个完整租约（2s）后从另一个调用触发回收。
        time.sleep(2.6)
        reclaimed = _repo(db).reclaim_expired_claims()
        assert reclaimed == 0, f"live heartbeat should keep lease out of reclaim: {reclaimed}"
        assert _approval_row(db, aid)["status"] == "in_progress"

        worker.join(timeout=20)
        status, body = results[0]
        assert status == 200, body
        assert body["status"] == "done" and body["executed"] is True, body

        lines = _counter_lines(counter)
        assert lines.count("invoked") == 1, lines
        assert lines.count("finished") == 1, lines
        assert _approval_row(db, aid)["consumed"] is True
        assert _paused_row(db, tenant, task_id) is None
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- case 6: without the heartbeat the lease still expires ------------------

def test_without_heartbeat_lease_expires_mid_run(db, tmp_path):
    """心跳关闭时租约照常过期：运行中被回收，complete 条件更新 0 行 → 409。"""
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 3.0, lease_seconds=2, heartbeat=False).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        results: list[tuple[int, dict]] = []

        def _resume():
            results.append(_post(app.base + f"/chat/{task_id}/resume",
                                 {"tenant_id": tenant}))

        worker = threading.Thread(target=_resume, daemon=True)
        worker.start()
        assert _wait_for_line(counter, "invoked", timeout=10), "tool never started"

        time.sleep(2.6)
        reclaimed = _repo(db).reclaim_expired_claims()
        assert reclaimed >= 1, reclaimed
        assert _approval_row(db, aid)["status"] == "approved"

        worker.join(timeout=20)
        status, body = results[0]
        assert status == 409, body
        assert body["detail"] == "approval lease lost during execution", body
        assert _approval_row(db, aid)["consumed"] is False

        # 回收后可重试：这次没有心跳干扰，正常 done。
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 200, body
        assert body["status"] == "done" and body["executed"] is True, body

        lines = _counter_lines(counter)
        assert lines.count("invoked") == 2, lines  # 原始执行 + 1 次重放
        assert lines.count("finished") == 2, lines
        assert _approval_row(db, aid)["consumed"] is True
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)


# --- case 7: a stuck tool times out into a terminal failed state ------------

def test_stuck_tool_times_out_instead_of_heartbeat_forever(db, tmp_path):
    """工具卡死 6s、超时 1s：心跳虽然续期也只能撑到超时，之后行必须 failed。

    反证「心跳会让卡死工具永远停在 in_progress」：超时是终态，心跳随 finally 停止。
    """
    tenant = _tenant()
    counter = str(tmp_path / "counter.txt")
    app = UvicornProc(counter, 6.0, lease_seconds=2, timeout_seconds=1.0).start()
    task_id = aid = None
    try:
        task_id, aid = _pause_for_approval(app, tenant)
        status, body = _approve(app, aid, tenant)
        assert status == 200 and body["status"] == "approved", body

        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 504, body
        assert body["detail"] == "approved action timed out", body

        row = db.fetch_one(
            "SELECT status, consumed, last_error FROM agent_approval WHERE id = ?",
            (aid,))
        assert row["status"] == "failed", row
        assert row["consumed"] is False, row
        assert row["last_error"] == "tool timeout", row

        # 心跳已停：把租约快进到过期再回收，failed 是终态，不会被改回 approved。
        db.execute("UPDATE agent_approval SET lease_until = now() - INTERVAL '1 second' "
                   "WHERE id = ?", (aid,))
        _repo(db).reclaim_expired_claims()
        row = db.fetch_one(
            "SELECT status, consumed FROM agent_approval WHERE id = ?", (aid,))
        assert row["status"] == "failed" and row["consumed"] is False, row

        # failed 是终态：再次 resume 是 409，且不会重放工具。
        status, body = _post(app.base + f"/chat/{task_id}/resume", {"tenant_id": tenant})
        assert status == 409, body
        assert "not approved" in body.get("detail", ""), body
        assert _counter_lines(counter).count("invoked") == 1
    finally:
        app.stop()
        _cleanup(db, tenant, task_id, aid)

