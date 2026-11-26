"""tests/test_memory_pg.py: Task 1 + Task 2 + Task 3 + Task 4 + Task 5 - connector + schema + MemoryStore + MemoryProviders + loop wiring."""
import pytest


def test_inmemory_connector_crud_and_healthcheck():
    from infrastructure.pg.connector import InMemoryConnector
    c = InMemoryConnector()
    c.execute("CREATE TABLE x (id INT, name TEXT)")
    c.execute("INSERT INTO x VALUES (?, ?)", (1, "a"))
    rows = c.fetch_all("SELECT * FROM x")
    assert rows == [{"id": 1, "name": "a"}]
    one = c.fetch_one("SELECT * FROM x WHERE id = ?", (1,))
    assert one == {"id": 1, "name": "a"}
    assert c.healthcheck() is True


def test_strict_persistence_requires_agent_dsn(monkeypatch):
    from infrastructure.pg.connector import OperationalError, get_agent_connector

    for name in ("AGENT_PG_DSN", "AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "true")

    with pytest.raises(OperationalError, match="AGENT_PG_DSN"):
        get_agent_connector()


def test_strict_persistence_rejects_offline_override(monkeypatch):
    from infrastructure.pg.connector import OperationalError, get_agent_connector

    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://unused@127.0.0.1/db_agent")
    monkeypatch.setenv("AGENT_PG_OFFLINE", "1")
    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "1")

    with pytest.raises(OperationalError, match="AGENT_PG_DSN"):
        get_agent_connector()


def test_strict_persistence_fails_fast_when_postgres_is_unreachable(monkeypatch):
    import infrastructure.pg.connector as conn_mod

    for name in ("AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://unused@127.0.0.1/db_agent")
    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "true")

    class UnhealthyPG:
        def __init__(self, dsn, pool_size):
            pass

        def healthcheck(self):
            return False

    monkeypatch.setattr(conn_mod, "PGConnector", UnhealthyPG)
    with pytest.raises(conn_mod.OperationalError, match="healthcheck failed"):
        conn_mod.get_agent_connector()


def test_strict_persistence_does_not_leak_connection_error(monkeypatch):
    import infrastructure.pg.connector as conn_mod

    for name in ("AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://unused@127.0.0.1/db_agent")
    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "true")

    class BrokenPG:
        def __init__(self, dsn, pool_size):
            raise RuntimeError("password=do-not-leak")

    monkeypatch.setattr(conn_mod, "PGConnector", BrokenPG)
    with pytest.raises(conn_mod.OperationalError) as exc:
        conn_mod.get_agent_connector()
    assert "do-not-leak" not in str(exc.value)


def test_strict_persistence_returns_healthy_postgres(monkeypatch):
    import infrastructure.pg.connector as conn_mod
    from infrastructure.pg import schema as schema_mod

    for name in ("AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://unused@127.0.0.1/db_agent")
    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "true")
    connector = object()

    class HealthyPG:
        def __init__(self, dsn, pool_size):
            self.connector = connector

        def healthcheck(self):
            return True

    monkeypatch.setattr(conn_mod, "PGConnector", HealthyPG)
    monkeypatch.setattr(schema_mod, "ensure_schema", lambda c: None)
    assert conn_mod.get_agent_connector().connector is connector


def test_strict_persistence_rejects_schema_initialization_failure(monkeypatch):
    import warnings

    import infrastructure.pg.connector as conn_mod
    from infrastructure.pg import schema as schema_mod

    for name in ("AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://unused@127.0.0.1/db_agent")
    monkeypatch.setenv("AGENT_STRICT_PERSISTENCE", "true")

    class HealthyPG:
        def __init__(self, dsn, pool_size):
            pass

        def healthcheck(self):
            return True

    def broken_schema(connector):
        warnings.warn("permission denied while creating agent schema", stacklevel=2)

    monkeypatch.setattr(conn_mod, "PGConnector", HealthyPG)
    monkeypatch.setattr(schema_mod, "ensure_schema", broken_schema)
    with pytest.raises(conn_mod.OperationalError, match="schema initialization failed"):
        conn_mod.get_agent_connector()


def test_pg_healthcheck_is_bounded_and_does_not_retry(monkeypatch):
    import sqlalchemy

    import infrastructure.pg.connector as conn_mod

    captured = {}

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("connection timed out")

    def fake_create_engine(dsn, **kwargs):
        captured.update(kwargs)
        return BrokenEngine()

    def unexpected_sleep(seconds):
        pytest.fail("healthcheck must not retry or sleep")

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    monkeypatch.setattr(conn_mod.time, "sleep", unexpected_sleep)
    connector = conn_mod.PGConnector("postgresql://unused@127.0.0.1/db_agent")

    assert captured["connect_args"] == {"connect_timeout": 2}
    assert connector.healthcheck() is False


def test_non_strict_persistence_keeps_offline_fallback(monkeypatch):
    import infrastructure.pg.connector as conn_mod

    for name in ("AGENT_PG_OFFLINE", "PG_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://unused@127.0.0.1/db_agent")
    monkeypatch.delenv("AGENT_STRICT_PERSISTENCE", raising=False)

    class UnhealthyPG:
        def __init__(self, dsn, pool_size):
            pass

        def healthcheck(self):
            return False

    monkeypatch.setattr(conn_mod, "PGConnector", UnhealthyPG)
    with pytest.warns(UserWarning, match="falling back to in-memory"):
        assert conn_mod.get_agent_connector() is None


def test_ensure_schema_idempotent_on_inmemory(monkeypatch):
    from infrastructure.pg.connector import InMemoryConnector
    from infrastructure.pg.schema import ensure_schema
    c = InMemoryConnector()
    ensure_schema(c)
    ensure_schema(c)
    rows = c.fetch_all("SELECT 1")
    assert rows is not None


def test_memory_store_tenant_isolation_and_permission_filter():
    from agent.memory.store import MemoryStore
    from infrastructure.pg.connector import InMemoryConnector
    c = InMemoryConnector()
    c.execute("CREATE TABLE memory_user (id INT, tenant_id TEXT, permission TEXT, body TEXT)")
    s = MemoryStore(c)
    s.put("user", "tenant_a", {"name": "alice"}, permission="public")
    s.put("user", "tenant_b", {"name": "bob"}, permission="public")
    rows = s.query("user", "tenant_a", permission="public")
    assert len(rows) == 1
    assert rows[0]["body"]["name"] == "alice"


def test_memory_store_query_accepts_predecoded_jsonb_body():
    """pgvector/psycopg 路径会把 JSONB 直接返回成 dict，不能再 json.loads 一次。"""
    from agent.memory.store import MemoryStore
    from infrastructure.pg.connector import InMemoryConnector
    c = InMemoryConnector()
    c.execute("CREATE TABLE memory_user (id INT, tenant_id TEXT, permission TEXT, body TEXT)")
    s = MemoryStore(c)
    s.put("user", "tenant_a", {"name": "alice"}, permission="public")
    # 模拟 psycopg 返回已解码的 JSONB
    for row in c._tables["memory_user"]:
        row["body"] = {"name": "alice"}
    rows = s.query("user", "tenant_a", permission="public")
    assert len(rows) == 1
    assert rows[0]["body"]["name"] == "alice"


def test_memory_store_outage_degrades_silently(monkeypatch):
    from agent.memory.store import MemoryStore
    from infrastructure.pg.connector import InMemoryConnector, OperationalError
    c = InMemoryConnector()
    s = MemoryStore(c)
    def boom(sql, params=()):
        raise OperationalError("pg down")
    monkeypatch.setattr(c, "execute", boom)
    s.put("user", "tenant_a", {"x": 1})  # 不抛
    assert s.query("user", "tenant_a", permission="public") == []


def test_memory_store_put_warns_on_outage():
    import warnings

    from agent.memory.store import MemoryStore
    from infrastructure.pg.connector import InMemoryConnector, OperationalError
    c = InMemoryConnector()
    s = MemoryStore(c)
    def boom(sql, params=()):
        raise OperationalError("pg down")
    c.execute = boom  # 直接覆盖
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        s.put("user", "tenant_a", {"x": 1})
    assert any("memory put outage" in str(w.message) and "user" in str(w.message)
               for w in captured), f"no outage warning captured, got: {[str(w.message) for w in captured]}"


def test_memory_providers_collect_from_store_and_format(monkeypatch):
    import infrastructure.pg.connector as conn_mod
    from agent.context.memory_providers import (
        ConvMemoryProvider,
        EpisodicMemoryProvider,
        OrgMemoryProvider,
        UserMemoryProvider,
    )
    from agent.memory.store import MemoryStore
    from infrastructure.pg.connector import InMemoryConnector
    c = InMemoryConnector()
    monkeypatch.setattr(conn_mod, "get_connector", lambda: c)
    for t in ("memory_conv", "memory_user", "memory_org"):
        c.execute(f"CREATE TABLE {t} (id INT, tenant_id TEXT, permission TEXT, body TEXT)")
    s = MemoryStore(c)
    s.put("conv", "t1", {"role": "user", "content": "hi"}, permission="tenant")
    s.put("user", "t1", {"name": "alice", "role": "ops"}, permission="tenant")
    s.put("org", "t1", {"policy": "审批>5000走财务"}, permission="tenant")
    state = {"tenant_id": "t1"}
    conv_segs = ConvMemoryProvider(permission="tenant").collect(state)
    user_segs = UserMemoryProvider(permission="tenant").collect(state)
    org_segs = OrgMemoryProvider(permission="tenant").collect(state)
    epi_segs = EpisodicMemoryProvider(permission="tenant").collect(state)
    assert any("hi" in s["text"] for s in conv_segs)
    assert any("alice" in s["text"] for s in user_segs)
    assert any("审批" in s["text"] for s in org_segs)
    assert epi_segs == []


def test_memory_provider_outage_raises_memory_outage(monkeypatch):
    from agent.context.memory_providers import ConvMemoryProvider, MemoryOutage
    from agent.memory.store import MemoryStore
    from infrastructure.pg.connector import InMemoryConnector, OperationalError
    c = InMemoryConnector()
    c.execute("CREATE TABLE memory_conv (id INT, tenant_id TEXT, permission TEXT, body TEXT)")
    s = MemoryStore(c)
    def boom(sql, params=()):
        raise OperationalError("pg down")
    monkeypatch.setattr(c, "execute", boom)
    monkeypatch.setattr(c, "fetch_all", lambda sql, params=(): (_ for _ in ()).throw(OperationalError("pg down")))
    p = ConvMemoryProvider(store=s, permission="tenant")
    state = {"tenant_id": "t1"}
    import pytest
    with pytest.raises(MemoryOutage):
        p.collect(state)


def test_loop_outage_skips_memory_layer_in_report(monkeypatch):
    import infrastructure.pg.connector as conn_mod
    from agent.context.assembler import ContextAssembler
    from agent.context.budget import TokenBudget
    from agent.context.memory_providers import ConvMemoryProvider
    from agent.memory.store import MemoryStore
    from agent.runtime.loop import run
    from agent.runtime.state import AgentState
    from infrastructure.pg.connector import InMemoryConnector, OperationalError

    c = InMemoryConnector()
    c.execute("CREATE TABLE memory_conv (id INT, tenant_id TEXT, permission TEXT, body TEXT)")
    monkeypatch.setattr(conn_mod, "get_connector", lambda: c)
    def boom(sql, params=()):
        raise OperationalError("pg down")
    monkeypatch.setattr(c, "execute", boom)
    monkeypatch.setattr(c, "fetch_all", lambda sql, params=(): (_ for _ in ()).throw(OperationalError("pg down")))

    asm = ContextAssembler(budget=TokenBudget(total=400, reserved_for_output=0),
                           providers=[ConvMemoryProvider(MemoryStore(c))])
    st = AgentState(task="x")
    st.context["tenant_id"] = "t1"
    def planner(state):
        return {"action": "finish", "answer": "ok"}
    run(st, planner=planner, assembler=asm, executor=lambda p, st: None)
    rep = st.context.get("context_report", {})
    assert any(s.startswith("memory_") for s in rep.get("skipped", []))
