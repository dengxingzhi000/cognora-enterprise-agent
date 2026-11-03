"""Batch 7: 合同合规 / 销售分析真实流程（fixture 驱动，不伪造外部返回）。"""
import json
from pathlib import Path

from workflow.scenarios import analyze_sales, review_contract

DATA_DIR = Path(__file__).resolve().parent.parent / "workflow" / "data"


def test_review_contract_from_fixture_structures_findings():
    out = review_contract(DATA_DIR / "contract_risky.json")
    assert out["decision"] == "human_review"
    assert "赔偿" in out["opinion"]
    types = {f["type"] for f in out["findings"]}
    assert "risky_clause" in types
    assert "amount_threshold" in types
    assert out["supplier"]["supplier_id"] == "SP-001"
    assert "scm.supplier.get" in out["trace"]
    assert out["policy_ref"]


def test_review_contract_clean_fixture_auto_approves():
    out = review_contract(DATA_DIR / "contract_clean.json")
    assert out["decision"] == "auto_approve"
    assert out["findings"] == []
    assert out["missing"] == []


def test_review_contract_missing_data_is_insufficient_data():
    out = review_contract({"clauses": ["标准条款"]})
    assert out["decision"] == "insufficient_data"
    assert out["missing"] == ["amount"]
    assert out["findings"] == []
    assert out["supplier"] is None

    out2 = review_contract({"amount": 1000})
    assert out2["decision"] == "insufficient_data"
    assert "clauses" in out2["missing"]


def test_analyze_sales_from_fixture_returns_structured_report():
    out = analyze_sales("这个月销售额为什么下降")
    assert out["status"] == "ok"
    assert out["source"].endswith("sales_week.json")
    metrics = out["metrics"]
    assert metrics["gmv"] > 0
    assert metrics["orders"] > 0
    types = {f["type"] for f in out["findings"]}
    assert "gmv_drop" in types
    assert "payment_failure" in types
    assert "metrics.get" in out["trace"]
    assert "knowledge.search" in out["trace"]
    assert "报告" in out["report"]
    assert out["recommendation"]


def test_analyze_sales_missing_file_degrades():
    out = analyze_sales("销售额为什么下降", data_path="does-not-exist.json")
    assert out["status"] == "insufficient_data"
    assert out["metrics"] == {}
    assert out["findings"] == []
    assert out["trace"] == []


def test_analyze_sales_empty_rows_degrades(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"rows": []}), encoding="utf-8")
    out = analyze_sales("为什么下降", data_path=path)
    assert out["status"] == "insufficient_data"
    assert "没有数据行" in out["report"]


def test_analyze_sales_accepts_plain_list_fixture(tmp_path):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps([
        {"category": "数码", "gmv": 1000, "orders": 10},
        {"category": "数码", "gmv": 500, "orders": 5},
    ]), encoding="utf-8")
    out = analyze_sales("分析", data_path=path)
    assert out["status"] == "ok"
    assert out["metrics"]["gmv"] == 1500
    assert out["metrics"]["orders"] == 15
