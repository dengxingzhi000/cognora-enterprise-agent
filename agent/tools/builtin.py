"""Built-in knowledge and analysis tools; operational mocks are opt-in."""
import logging
import os
from typing import Literal

from pydantic import BaseModel

from .registry import Registry, Tool


class MetricsArgs(BaseModel):
    service: str = "order"
    tenant_id: str | None = None


class LogsArgs(BaseModel):
    keyword: str = "5xx"
    query: str | None = None
    tenant_id: str | None = None


class GitArgs(BaseModel):
    repo: str | None = None


class DbQueryArgs(BaseModel):
    scope: Literal["self", "dept", "all"] = "self"
    tenant_id: str | None = None


class KnowledgeArgs(BaseModel):
    query: str
    tenant_id: str = "t1"
    # Injected by guarded_executor from the authenticated role, never trusted
    # from planner output when the guarded execution path is used.
    allowed_permissions: list[str] | None = None


def _mock_enabled() -> bool:
    return os.environ.get("AGENT_ENABLE_MOCK_TOOLS", "").strip().lower() in {"1", "true", "yes", "on"}


def _require_mock(name: str) -> None:
    if not _mock_enabled():
        raise RuntimeError(f"UNSUPPORTED_TOOL: {name} has no configured data source")


def _metrics_get(args: dict):
    _require_mock("metrics.get")
    service = args.get("service", "order")
    return f"{service} 5xx rate 12% (p99 latency 2.3s), window 15m"


def _logs_search(args: dict):
    _require_mock("logs.search")
    keyword = args.get("keyword", "5xx")
    return f"ERROR order-api {keyword} Timeout upstream payment-service x42, trace_id=abc123"


def _git_diff(args: dict):
    _require_mock("git.diff")
    return "recent commit 9f3a2b: payment-service timeout 2s->500ms, order-api retry x3"


def _db_query(args: dict):
    _require_mock("db.query")
    return "row1: order_no=20260915001 status=PAID amount=6230"


def _knowledge_search(args: dict):
    from integrations.scm.context import current_scm_context
    from knowledge.service import get_knowledge_service

    q = args.get("query", "")
    identity = current_scm_context()
    service = get_knowledge_service()
    if identity is not None:
        hits = service.search(q, identity.tenant_id, identity.role)
    else:
        hits = service.store.search(
            q, tenant_id=args.get("tenant_id", "t1"),
            allowed_permissions=args.get("allowed_permissions"),
        )
    if not hits:
        return "no hits"
    return "\n".join(f"[{h.document_id}#{h.chunk_id} {h.permission}] {h.text[:200]}" for h in hits)


def build_default_registry() -> Registry:
    reg = Registry()
    reg.register(Tool("metrics.get", "查服务错误率/延迟", _metrics_get, MetricsArgs))
    reg.register(Tool("logs.search", "查错误日志", _logs_search, LogsArgs))
    reg.register(Tool("git.diff", "查最近提交", _git_diff, GitArgs))
    reg.register(Tool("db.query", "查业务数据(mock)", _db_query, DbQueryArgs))
    reg.register(Tool("knowledge.search", "查企业制度/手册(RAG)", _knowledge_search, KnowledgeArgs))
    from .analysis import register_analysis_tools
    register_analysis_tools(reg)
    try:
        from integrations.scm.tools import register_scm_tools
        register_scm_tools(reg)
    except Exception as e:
        logging.getLogger(__name__).warning("scm tools auto-register failed: %s", e, exc_info=True)
    return reg
