"""三场景复用同一套件：合同合规 + 销售分析（运维已在Stage2/6覆盖）。

合同与销售都用真实输入（仓库内样例 JSON / 调用方传入 dict）驱动：
- 合同：条款规则 + 金额阈值 + 知识库制度检索 + supplier 风险工具；
- 销售：本地样例数据聚合 → 指标/知识库工具补充 → 结构化结论。
缺数据时返回 ``insufficient_data``，不伪造外部系统返回。
"""
import json
import logging
import re
from pathlib import Path

RISKY_CLAUSES = ("无限赔偿", "预付款100%", "口头承诺", "独家买断")
DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_json(value, default_path: str | Path | None = None):
    """dict 原样返回；字符串按路径读取；None 读默认样例。"""
    if isinstance(value, dict):
        return dict(value), None
    path = Path(value) if value else (Path(default_path) if default_path else None)
    if path is None:
        return None, "no input"
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 - 缺文件/坏 JSON 走 insufficient_data
        return None, f"cannot read {path}: {e}"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    return data, None


def review_contract(contract: dict | str | None, tenant_id: str = "t1") -> dict:
    """合同合规：输入校验 → 条款/金额规则 → 制度检索 → supplier 风险 → 结论。"""
    data, error = _load_json(contract)
    if data is None:
        return {"decision": "insufficient_data", "opinion": f"合同数据不可用：{error}",
                "missing": ["contract"], "findings": [], "supplier": None, "trace": []}

    missing: list[str] = []
    amount = data.get("amount")
    clauses = data.get("clauses")
    if amount is None:
        missing.append("amount")
    if clauses is None:
        missing.append("clauses")
    if missing:
        return {"decision": "insufficient_data",
                "opinion": f"合同缺少必填字段：{', '.join(missing)}",
                "missing": missing, "findings": [], "supplier": None, "trace": []}
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"decision": "insufficient_data", "opinion": "合同金额不是数字",
                "missing": ["amount"], "findings": [], "supplier": None, "trace": []}
    if not isinstance(clauses, list):
        clauses = [str(clauses)]
    supplier_id = data.get("supplier_id", "")

    from knowledge.seed import get_default_store
    hits = get_default_store().search("合同审批 赔偿 预付款", tenant_id=tenant_id,
                                      allowed_permissions=["employee", "finance", "manager", "admin"])
    policy_ref = hits[0].text[:120] if hits else "超阈值需审批"
    trace = ["retrieve_policy", "judge_rule"]

    findings: list[dict] = []
    for clause in clauses:
        matched = [rule for rule in RISKY_CLAUSES if rule in str(clause)]
        if matched:
            findings.append({"type": "risky_clause", "clause": str(clause),
                             "rules": matched, "severity": "high"})
    if amount > 10000:
        findings.append({"type": "amount_threshold", "amount": amount,
                         "threshold": 10000, "severity": "medium"})

    if not findings:
        return {"decision": "auto_approve",
                "opinion": f"标准小额合同自动通过：金额{amount:g}",
                "missing": [], "findings": [], "supplier": None,
                "policy_ref": policy_ref,
                "input": {"amount": amount, "clause_count": len(clauses)},
                "trace": trace + ["auto_approve"]}

    supplier_info = "(supplier 信息暂不可用)"
    supplier = None
    if supplier_id:
        try:
            from agent.tools.builtin import build_default_registry
            reg = build_default_registry()
            sup_out = reg.call("scm.supplier.get", {"supplier_id": supplier_id,
                                                   "tenant_id": tenant_id})
            supplier_info = str(sup_out)
            supplier = {"supplier_id": supplier_id, "data": supplier_info}
            trace.append("scm.supplier.get")
        except Exception as e:
            logging.getLogger(__name__).warning("scm.supplier.get failed: %r", e)

    return {"decision": "human_review",
            "opinion": f"风险条款需人审：{clauses}；金额{amount:g}；供应商{supplier_info}。依据：{policy_ref}",
            "missing": [], "findings": findings, "supplier": supplier,
            "policy_ref": policy_ref,
            "input": {"amount": amount, "clause_count": len(clauses)},
            "trace": trace + ["human_review"]}


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _group_sales(rows: list[dict]) -> dict:
    """按日期/顺序二分窗口，算 GMV、订单、类目与支付失败变化。"""
    n = len(rows)
    half = max(1, n // 2)
    first, last = rows[:half], rows[half:] if n > half else rows[-half:]

    def totals(part):
        return {
            "gmv": sum(_num(r, "gmv") for r in part),
            "orders": sum(_num(r, "orders") for r in part),
            "failed_payments": sum(_num(r, "failed_payments") for r in part),
        }

    t1, t2 = totals(first), totals(last)
    by_cat: dict[str, dict] = {}
    for part, key in ((first, "first"), (last, "last")):
        for r in part:
            cat = str(r.get("category") or "未分类")
            slot = by_cat.setdefault(cat, {"first": 0.0, "last": 0.0})
            slot[key] += _num(r, "gmv")
    cat_changes = []
    for cat, v in by_cat.items():
        change = ((v["last"] - v["first"]) / v["first"] * 100.0) if v["first"] else 0.0
        cat_changes.append({"category": cat, "first_gmv": v["first"],
                            "last_gmv": v["last"], "change_pct": round(change, 2)})

    total_gmv = t1["gmv"] + t2["gmv"]
    total_orders = t1["orders"] + t2["orders"]
    change_pct = ((t2["gmv"] - t1["gmv"]) / t1["gmv"] * 100.0) if t1["gmv"] else 0.0
    worst = min(cat_changes, key=lambda c: c["change_pct"]) if cat_changes else None
    return {
        "gmv": round(total_gmv, 2),
        "orders": int(total_orders),
        "aov": round(total_gmv / total_orders, 2) if total_orders else 0.0,
        "window_first_gmv": round(t1["gmv"], 2),
        "window_last_gmv": round(t2["gmv"], 2),
        "change_pct": round(change_pct, 2),
        "failed_payments_first": int(t1["failed_payments"]),
        "failed_payments_last": int(t2["failed_payments"]),
        "categories": cat_changes,
        "worst_category": worst,
    }


def analyze_sales(question: str, tenant_id: str = "t1",
                  data_path: str | Path | None = None) -> dict:
    """销售分析：样例数据聚合 → metrics/knowledge 工具 → 结构化结论。"""
    rows: list[dict] | None = None
    source = Path(data_path) if data_path else DATA_DIR / "sales_week.json"
    try:
        with source.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            raw_rows = payload.get("rows") or []
        elif isinstance(payload, list):
            raw_rows = payload
        else:
            raw_rows = []
        rows = [r for r in raw_rows if isinstance(r, dict)]
    except Exception as e:  # noqa: BLE001
        return {"status": "insufficient_data",
                "report": f"销售分析无法完成：缺少可用数据（{source}: {e}）",
                "metrics": {}, "findings": [], "trace": [], "source": str(source)}
    if not rows:
        return {"status": "insufficient_data",
                "report": f"销售分析无法完成：{source} 没有数据行",
                "metrics": {}, "findings": [], "trace": [], "source": str(source)}

    from agent.tools.builtin import build_default_registry
    reg = build_default_registry()
    trace = ["load_sales_fixture"]
    metrics = _group_sales(rows)

    try:
        ops_metrics = reg.call("metrics.get", {"service": "mall"})
        trace.append("metrics.get")
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("metrics.get failed: %r", e)
        ops_metrics = "metrics 暂不可用"

    try:
        kb = reg.call("knowledge.search", {"query": question, "tenant_id": tenant_id})
        trace.append("knowledge.search")
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("knowledge.search failed: %r", e)
        kb = "知识库暂不可用"

    findings: list[dict] = []
    if metrics["change_pct"] <= -5:
        findings.append({"type": "gmv_drop", "change_pct": metrics["change_pct"],
                         "first_gmv": metrics["window_first_gmv"],
                         "last_gmv": metrics["window_last_gmv"]})
    worst = metrics.get("worst_category")
    if worst and worst["change_pct"] <= -5:
        findings.append({"type": "category_drop", **worst})
    failed_delta = metrics["failed_payments_last"] - metrics["failed_payments_first"]
    if failed_delta > 0:
        findings.append({"type": "payment_failure",
                         "first": metrics["failed_payments_first"],
                         "last": metrics["failed_payments_last"],
                         "delta": failed_delta})

    if any(f["type"] == "payment_failure" for f in findings):
        recommendation = "优先排查支付超时/失败（failed_payments 上升），再按类目补货与投放。"
    elif worst:
        recommendation = f"优先复盘类目「{worst['category']}」的下滑原因，并结合知识库 SOP 排查。"
    else:
        recommendation = "未发现显著下滑，保持现有投放节奏并持续观察。"

    report = (
        f"销售分析报告：{question}\n"
        f"- GMV：{metrics['gmv']}，订单：{metrics['orders']}，客单价：{metrics['aov']}\n"
        f"- 窗口变化：{metrics['change_pct']}%"
        f"（{metrics['window_first_gmv']} → {metrics['window_last_gmv']}）\n"
        f"- 支付失败：{metrics['failed_payments_first']} → {metrics['failed_payments_last']}\n"
        f"- 类目最差：{worst['category'] + ' ' + str(worst['change_pct']) + '%' if worst else '无'}\n"
        f"- 运维指标：{ops_metrics}\n"
        f"- 知识库：{str(kb)[:300]}\n"
        f"- 建议：{recommendation}"
    )
    return {"status": "ok", "report": report, "metrics": metrics,
            "findings": findings, "recommendation": recommendation,
            "trace": trace, "source": str(source)}


_ORDER_PATTERN = re.compile(r"\bO-\d{6,}\b")


def _try_or_knowledge(reg, primary_name: str, primary_args: dict, fallback_args: dict,
                      trace: list, placeholder: str) -> str:
    """Try primary tool; on exception, fall back to knowledge.search; on second failure, placeholder."""
    try:
        result = reg.call(primary_name, primary_args)
        trace.append(primary_name)
        return result
    except Exception as e:
        logging.getLogger(__name__).warning("%s failed: %r", primary_name, e)
        try:
            result = reg.call("knowledge.search", fallback_args)
            trace.append("knowledge.search")
            return result
        except Exception:
            return placeholder


def run_it_ops(question: str, tenant_id: str = "t1") -> dict:
    """IT Ops 场景：metrics + logs 模板；命中异常模式时拉订单详情。

    Scenario 内部 tool 调用不经 Policy 强制（设计 §5.3 显式 trade-off，
    与 analyze_sales 一致；scenario 自身已被 workflow 层授权）。
    """
    from agent.tools.builtin import build_default_registry
    reg = build_default_registry()
    trace = []
    order = "订单暂不可用"

    metrics = _try_or_knowledge(
        reg, "metrics.get", {"service": "mall"}, {"query": question, "tenant_id": tenant_id},
        trace, "metrics 暂不可用")
    logs = _try_or_knowledge(
        reg, "logs.search", {"query": question, "tenant_id": tenant_id},
        {"query": question, "tenant_id": tenant_id}, trace, "logs 暂不可用")

    logs_text = str(logs).lower()
    if any(k in logs_text for k in ("error", "timeout", "500", "失败")):
        m = _ORDER_PATTERN.search(question)
        if m and "scm.order.get" in reg.list_tools():
            try:
                order = reg.call("scm.order.get", {"order_no": m.group(0),
                                                   "tenant_id": tenant_id})
                trace.append("scm.order.get")
            except Exception as e:
                logging.getLogger(__name__).warning("scm.order.get failed: %r", e)
                order = "订单暂不可用"

    report = (
        f"IT Ops 诊断报告：{question}\n"
        f"- 服务指标：{metrics}\n"
        f"- 相关日志：{logs}\n"
        f"- 受影响订单：{order}\n"
        f"- 初步判断：依据日志模式 + 订单状态定位故障源。"
    )
    return {"report": report, "trace": trace}
