-- Enterprise Agent 自身持久化表
-- 库名：db_agent（独立于 scm-platform 业务库 db_*）
--
-- 初始化步骤（密码由环境变量注入，不要写进仓库）：
--   1) 以管理员连接 postgres：
--      psql "postgresql://<user>:<password>@192.168.0.121:5432/postgres" \
--           -c "CREATE DATABASE db_agent OWNER <user>;"
--   2) 建表：
--      psql "$AGENT_PG_DSN" -f scripts/db/agent_schema.sql
--   3) 应用配置：
--      export AGENT_PG_DSN="postgresql://<user>:<password>@192.168.0.121:5432/db_agent"
--      # AGENT_PG_DSN 是唯一开关；未设置时自动回落 InMemory
--
-- 说明：
-- - memory_* 表由 MemoryStore/EpisodicMemoryStore 读写；
-- - agent_approval / agent_paused_task 由审批与挂起任务 repository 读写；
-- - checkpoints/checkpoint_* 由 LangGraph PostgresSaver.setup() 自动创建（可选）。

CREATE TABLE IF NOT EXISTS agent_approval (
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
);

ALTER TABLE agent_approval
    ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;

ALTER TABLE agent_approval
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

ALTER TABLE agent_approval
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;

ALTER TABLE agent_approval
    ADD COLUMN IF NOT EXISTS claim_token UUID;

ALTER TABLE agent_approval
    ADD COLUMN IF NOT EXISTS claimed_by TEXT;

CREATE TABLE IF NOT EXISTS agent_tool_effect (
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
);

ALTER TABLE agent_tool_effect
    ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_tool_effect_approval
    ON agent_tool_effect(approval_id);

ALTER TABLE agent_approval
    ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_approval_tenant
    ON agent_approval(tenant_id, status);

CREATE TABLE IF NOT EXISTS agent_paused_task (
    tenant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_paused_task_tenant
    ON agent_paused_task(tenant_id);

-- 五层记忆：字段与 agent/memory/store.py + infrastructure/pg/schema.py 一致。
CREATE TABLE IF NOT EXISTS memory_user (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    department TEXT,
    permission TEXT NOT NULL DEFAULT 'public',
    version INT NOT NULL DEFAULT 1,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_user_tenant ON memory_user(tenant_id);
CREATE INDEX IF NOT EXISTS idx_memory_user_perm ON memory_user(permission);

CREATE TABLE IF NOT EXISTS memory_org (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    department TEXT,
    permission TEXT NOT NULL DEFAULT 'public',
    version INT NOT NULL DEFAULT 1,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_org_tenant ON memory_org(tenant_id);

CREATE TABLE IF NOT EXISTS memory_conv (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    department TEXT,
    permission TEXT NOT NULL DEFAULT 'public',
    version INT NOT NULL DEFAULT 1,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_conv_tenant ON memory_conv(tenant_id);

CREATE TABLE IF NOT EXISTS memory_task (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    department TEXT,
    permission TEXT NOT NULL DEFAULT 'public',
    version INT NOT NULL DEFAULT 1,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_task_tenant ON memory_task(tenant_id);

CREATE TABLE IF NOT EXISTS memory_episodic (
    id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    department TEXT,
    permission TEXT NOT NULL DEFAULT 'public',
    version INT NOT NULL DEFAULT 1,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_episodic_tenant ON memory_episodic(tenant_id);
