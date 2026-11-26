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
        claimed_at TIMESTAMPTZ,
        lease_until TIMESTAMPTZ,
        claim_token UUID,
        claimed_by TEXT,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS claim_token UUID""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS claimed_by TEXT""",
    """ALTER TABLE agent_approval ADD COLUMN IF NOT EXISTS last_error TEXT""",
    """CREATE INDEX IF NOT EXISTS idx_agent_approval_tenant ON agent_approval(tenant_id, status)""",
    # --- 内网 BFF 签名 nonce：跨进程防重放，过期行按请求惰性清理 ---
    """CREATE TABLE IF NOT EXISTS agent_internal_nonce (
        nonce TEXT PRIMARY KEY,
        expires_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_internal_nonce_expires
       ON agent_internal_nonce(expires_at)""",
    # --- 写工具幂等占位：同一 approval + 工具 + 参数只执行一次 ---
    """CREATE TABLE IF NOT EXISTS agent_tool_effect (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        approval_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        args_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 1,
        result_ref TEXT,
        maybe_side_effect BOOLEAN NOT NULL DEFAULT FALSE,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, approval_id, tool_name, args_hash)
    )""",
    """ALTER TABLE agent_tool_effect ADD COLUMN IF NOT EXISTS last_error TEXT""",
    """CREATE INDEX IF NOT EXISTS idx_agent_tool_effect_approval
       ON agent_tool_effect(approval_id)""",
    """CREATE TABLE IF NOT EXISTS agent_paused_task (
        tenant_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        state JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id, task_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_paused_task_tenant ON agent_paused_task(tenant_id)""",
    """CREATE TABLE IF NOT EXISTS agent_conversation_message (
        id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_conversation_scope
       ON agent_conversation_message(tenant_id, user_id, session_id, created_at)""",
    """CREATE TABLE IF NOT EXISTS agent_async_task (
        task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
        session_id TEXT, status TEXT NOT NULL, result JSONB, error TEXT, owner TEXT,
        external_task_id TEXT, payload JSONB, lease_until TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS owner TEXT""",
    """ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS external_task_id TEXT""",
    """ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ""",
    """ALTER TABLE agent_async_task ADD COLUMN IF NOT EXISTS payload JSONB""",
    """CREATE INDEX IF NOT EXISTS idx_agent_async_task_scope
       ON agent_async_task(tenant_id, user_id, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS agent_quota_counter (
        tenant_id TEXT NOT NULL, window_name TEXT NOT NULL,
        window_start TIMESTAMPTZ NOT NULL, count BIGINT NOT NULL,
        PRIMARY KEY (tenant_id, window_name)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_trace (
        trace_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_trace_tenant ON agent_trace(tenant_id, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS agent_assistance_prediction (
        prediction_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'unknown',
        session_id TEXT NOT NULL,
        signals JSONB NOT NULL,
        recent_events JSONB NOT NULL DEFAULT '[]'::jsonb,
        history_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        server_facts_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        knowledge_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
        probability DOUBLE PRECISION NOT NULL,
        threshold DOUBLE PRECISION NOT NULL,
        decision TEXT NOT NULL,
        model_version TEXT NOT NULL,
        feature_version INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """ALTER TABLE agent_assistance_prediction ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'unknown'""",
    """ALTER TABLE agent_assistance_prediction ADD COLUMN IF NOT EXISTS recent_events JSONB NOT NULL DEFAULT '[]'::jsonb""",
    """ALTER TABLE agent_assistance_prediction ADD COLUMN IF NOT EXISTS history_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb""",
    """ALTER TABLE agent_assistance_prediction ADD COLUMN IF NOT EXISTS server_facts_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb""",
    """ALTER TABLE agent_assistance_prediction ADD COLUMN IF NOT EXISTS knowledge_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb""",
    """CREATE INDEX IF NOT EXISTS idx_agent_assistance_prediction_scope
       ON agent_assistance_prediction(tenant_id, user_id, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS agent_assistance_feedback (
        prediction_id TEXT PRIMARY KEY REFERENCES agent_assistance_prediction(prediction_id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        needed_assistance BOOLEAN NOT NULL,
        assistance_used BOOLEAN NOT NULL,
        task_succeeded BOOLEAN NOT NULL,
        source TEXT NOT NULL,
        knowledge_helpful BOOLEAN,
        knowledge_article_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_assistance_feedback_tenant
       ON agent_assistance_feedback(tenant_id, updated_at DESC)""",
    """ALTER TABLE agent_assistance_feedback ADD COLUMN IF NOT EXISTS knowledge_helpful BOOLEAN""",
    """ALTER TABLE agent_assistance_feedback ADD COLUMN IF NOT EXISTS knowledge_article_id TEXT""",
    """CREATE TABLE IF NOT EXISTS agent_assistance_outcome (
        outcome_id TEXT PRIMARY KEY,
        prediction_id TEXT REFERENCES agent_assistance_prediction(prediction_id) ON DELETE CASCADE,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        module TEXT NOT NULL,
        workflow TEXT NOT NULL,
        outcome TEXT NOT NULL,
        error_code TEXT,
        latency_ms BIGINT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX IF NOT EXISTS idx_agent_assistance_outcome_scope
       ON agent_assistance_outcome(tenant_id, user_id, session_id, occurred_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_agent_assistance_outcome_prediction
       ON agent_assistance_outcome(prediction_id, occurred_at DESC)""",
]


def ensure_schema(conn) -> None:
    for ddl in SCHEMA_DDL:
        try:
            conn.execute(ddl)
        except Exception as e:  # noqa: BLE001 - schema 失败不挂
            warnings.warn(f"schema DDL failed: {e}", stacklevel=2)
