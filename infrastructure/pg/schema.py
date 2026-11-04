"""infrastructure/pg/schema.py: 五张 memory 表幂等 DDL。"""
import warnings


SCHEMA_DDL: list[str] = [
    """CREATE TABLE IF NOT EXISTS memory_user (
        id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, department TEXT,
        permission TEXT NOT NULL DEFAULT 'public', version INT NOT NULL DEFAULT 1,
        body JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_memory_user_tenant ON memory_user(tenant_id)""",
    """CREATE INDEX IF NOT EXISTS idx_memory_user_perm ON memory_user(permission)""",
    """CREATE TABLE IF NOT EXISTS memory_org (
        id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, department TEXT,
        permission TEXT NOT NULL DEFAULT 'public', version INT NOT NULL DEFAULT 1,
        body JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_memory_org_tenant ON memory_org(tenant_id)""",
    """CREATE TABLE IF NOT EXISTS memory_conv (
        id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, department TEXT,
        permission TEXT NOT NULL DEFAULT 'public', version INT NOT NULL DEFAULT 1,
        body JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_memory_conv_tenant ON memory_conv(tenant_id)""",
    """CREATE TABLE IF NOT EXISTS memory_task (
        id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, department TEXT,
        permission TEXT NOT NULL DEFAULT 'public', version INT NOT NULL DEFAULT 1,
        body JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_memory_task_tenant ON memory_task(tenant_id)""",
    """CREATE TABLE IF NOT EXISTS memory_episodic (
        id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, department TEXT,
        permission TEXT NOT NULL DEFAULT 'public', version INT NOT NULL DEFAULT 1,
        body JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_memory_episodic_tenant ON memory_episodic(tenant_id)""",
    # --- Agent 自身持久化（Batch 2：审批单 + 挂起任务） ---
    """CREATE TABLE IF NOT EXISTS agent_approval (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        user_id TEXT,
        user_json JSONB NOT NULL,
        tool TEXT NOT NULL,
        args JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'pending',
        approver TEXT,
        consumed BOOLEAN NOT NULL DEFAULT FALSE,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ""",
    """CREATE INDEX IF NOT EXISTS idx_agent_approval_tenant ON agent_approval(tenant_id, status)""",
    """CREATE TABLE IF NOT EXISTS agent_paused_task (
        tenant_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        state JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, task_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_paused_task_tenant ON agent_paused_task(tenant_id)""",
]


def ensure_schema(conn) -> None:
    for ddl in SCHEMA_DDL:
        try:
            conn.execute(ddl)
        except Exception as e:  # noqa: BLE001 - schema 失败不挂
            warnings.warn(f"schema DDL failed: {e}")
