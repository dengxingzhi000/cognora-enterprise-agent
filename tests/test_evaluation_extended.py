from evaluation.evaluator import evaluate_analysis_cases, evaluate_knowledge_cases
from knowledge.document import Document
from knowledge.ingestion import ingest
from knowledge.store import KnowledgeStore


def test_knowledge_eval_reports_recall_and_acl():
    store = KnowledgeStore(use_vector=False)
    store.add_many(ingest(Document(
        text="财务审批制度", tenant_id="t1", department="finance",
        document_id="finance", version="v1", permission="finance",
    )))
    report = evaluate_knowledge_cases([
        {"query": "财务审批", "allowed_permissions": ["finance"],
         "expected_documents": ["finance"]},
        {"query": "财务审批", "allowed_permissions": ["employee"],
         "expected_documents": []},
    ], store=store)
    assert report["recall_at_k"] == 0.5
    assert report["acl_violation"] == 0.0


def test_analysis_eval_checks_expected_findings():
    report = evaluate_analysis_cases([
        {"question": "analyse expense", "workflow": "expense",
         "expected_codes": ["human_approval_disabled"]},
    ])
    assert report["task_success"] == 1.0
