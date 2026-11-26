"""最小Eval：跑基准集，打出 TaskSuccess / PolicyViolation。"""
import time


def evaluate_expense_cases(cases: list[dict], policy_threshold: float = 5000) -> dict:
    from workflow.graph import run_expense_workflow
    passed = 0
    violations = 0
    t0 = time.perf_counter()
    for c in cases:
        out = run_expense_workflow(c["expense"], policy_threshold=policy_threshold)
        if out["decision"] == c["expected"]:
            passed += 1
        # 违规：在超标时自动通过
        amount = c["expense"].get("amount", 0)
        if out["decision"] == "auto_approve" and amount > policy_threshold:
            violations += 1
    total = len(cases) or 1
    return {
        "total": len(cases),
        "passed": passed,
        "task_success": passed / total,
        "policy_violation": violations / total,
        "avg_latency_ms": (time.perf_counter() - t0) * 1000 / total,
    }


def evaluate_knowledge_cases(cases: list[dict], store=None, top_k: int = 3) -> dict:
    from knowledge.seed import get_default_store
    store = store or get_default_store()
    hits = 0
    acl_violations = 0
    for case in cases:
        results = store.search(
            case["query"], tenant_id=case.get("tenant_id", "t1"),
            allowed_permissions=case["allowed_permissions"], top_k=top_k,
        )
        expected = set(case.get("expected_documents", []))
        if expected & {item.document_id for item in results}:
            hits += 1
        allowed = set(case.get("allowed_permissions", []))
        if any(item.permission not in allowed for item in results):
            acl_violations += 1
    total = len(cases) or 1
    return {
        "total": len(cases), "recall_at_k": hits / total,
        "acl_violation": acl_violations / total,
    }


def evaluate_analysis_cases(cases: list[dict]) -> dict:
    from analysis.graph import run_system_analysis
    passed = 0
    for case in cases:
        report = run_system_analysis(
            case["question"], tenant_id=case.get("tenant_id", "t1"),
            workflow=case.get("workflow"),
        )
        codes = {finding["code"] for finding in report.get("findings", [])}
        if set(case.get("expected_codes", [])) <= codes:
            passed += 1
    total = len(cases) or 1
    return {"total": len(cases), "passed": passed, "task_success": passed / total}


def context_quality_gate(baseline: dict, managed: dict, max_ratio: float = 0.7) -> dict:
    b_use, m_use = baseline.get("usage", 0) or 1, managed.get("usage") or 0
    ratio = m_use / b_use
    decision_match = baseline.get("decision") is not None and managed.get("decision") == baseline.get("decision")
    ok = decision_match and ratio <= max_ratio
    return {"pass": ok, "usage_ratio": ratio,
            "decision_match": decision_match}
