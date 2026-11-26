"""Default enterprise knowledge documents, loaded through the configured store."""
from .document import Document
from .ingestion import ingest


def seed_default_store(store, tenant_id: str | None = None, *, overwrite: bool = False) -> None:
    """Seed missing documents for an explicitly provisioned tenant."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("seed tenant_id is required")
    tenant_id = tenant_id.strip()
    existing = {
        (item["document_id"], item["version"])
        for item in store.list_documents(
            tenant_id, ["employee", "finance", "manager", "admin"],
        )
    }
    documents = [
        Document(
            text="差旅报销制度：单笔超过5000元需财务审批，超过10000元需部门经理+财务双审批。合规要求附发票与行程单。",
            tenant_id=tenant_id, department="finance", document_id="expense-policy",
            version="v1", permission="finance", created_at="2026-01-01T00:00:00+00:00",
        ),
        Document(
            text="普通员工午餐补贴20元/天，随工资发放，无需审批。",
            tenant_id=tenant_id, department="hr", document_id="lunch-policy",
            version="v1", permission="employee", created_at="2026-01-01T00:00:00+00:00",
        ),
        Document(
            text="订单5xx排查手册：先看metrics.get确认错误率，再logs.search查ERROR堆栈，再git.diff看最近变更。",
            tenant_id=tenant_id, department="ops", document_id="ops-runbook",
            version="v1", permission="employee", created_at="2026-01-01T00:00:00+00:00",
        ),
    ]
    for document in documents:
        if not overwrite and (document.document_id, document.version) in existing:
            continue
        store.replace_document(
            document.tenant_id, document.document_id, document.version,
            ingest(document),
        )


def get_default_store():
    """Return the same lazy, strictly configured store used by the API and tools."""
    from .service import get_knowledge_service

    return get_knowledge_service().store
