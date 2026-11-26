from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from knowledge.service import permissions_for_role

from .features import AssistanceSignals


class AssistanceKnowledgeService:
    def __init__(self, knowledge_service=None, playbook_path: str | Path | None = None):
        self._knowledge_service = knowledge_service
        self._playbook_path = (
            Path(playbook_path)
            if playbook_path
            else Path(str(files("agent.assistance").joinpath("data/intervention_playbooks.json")))
        )
        self._articles = self._load_articles()

    def _load_articles(self) -> list[dict]:
        payload = json.loads(self._playbook_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("intervention playbooks must be a list")
        return [item for item in payload if isinstance(item, dict)]

    def recommend(
        self,
        tenant_id: str,
        role: str,
        signals: AssistanceSignals,
        recent_events: list[dict],
        server_facts: dict,
    ) -> list[dict]:
        allowed = set(permissions_for_role(role))
        error_codes = {
            str(item.get("error_code"))
            for item in recent_events
            if item.get("error_code") is not None
        }
        if server_facts.get("server_timeout_count"):
            error_codes.add("TIMEOUT")
        if server_facts.get("server_error_count"):
            error_codes.add("SERVER_ERROR")
        if server_facts.get("server_rejection_count"):
            error_codes.add("BUSINESS_REJECTION")
        ranked = []
        for article in self._articles:
            if article.get("permission") not in allowed:
                continue
            modules = article.get("modules") or []
            workflows = article.get("workflows") or []
            article_errors = set(article.get("error_codes") or [])
            module_match = "*" in modules or signals.module in modules
            workflow_match = "*" in workflows or signals.workflow in workflows
            if not module_match and not workflow_match:
                continue
            score = 0
            score += 4 if signals.module in modules else 0
            score += 3 if signals.workflow in workflows else 0
            score += 2 * len(error_codes & article_errors)
            score += int(bool(article.get("requires_approval"))) if signals.high_risk_action else 0
            if score <= 0:
                continue
            ranked.append((score, article))
        ranked.sort(key=lambda item: (-item[0], item[1]["article_id"]))
        results = [self._public_article(article) for _, article in ranked[:3]]
        if len(results) >= 3 or self._knowledge_service is None:
            return results
        query = " ".join(
            [signals.module, signals.workflow, *sorted(error_codes), "runbook", "approval"]
        ).strip()
        try:
            chunks = self._knowledge_service.search(query, tenant_id, role, top_k=3)
        except Exception:
            return results
        known = {item["article_id"] for item in results}
        for chunk in chunks:
            article_id = f"knowledge:{chunk.document_id}:{chunk.version}"
            if article_id in known:
                continue
            results.append(
                {
                    "article_id": article_id,
                    "version": chunk.version,
                    "title": chunk.document_id,
                    "summary": chunk.text[:400],
                    "steps": [chunk.text[:400]],
                    "permission": chunk.permission,
                    "requires_human_confirmation": signals.high_risk_action,
                    "approval_hint": "按当前业务审批规则处理",
                    "source": "tenant-knowledge",
                    "updated_at": chunk.created_at,
                }
            )
            known.add(article_id)
            if len(results) >= 3:
                break
        return results

    @staticmethod
    def _public_article(article: dict) -> dict[str, Any]:
        return {
            "article_id": article["article_id"],
            "version": article["version"],
            "title": article["title"],
            "summary": article["summary"],
            "steps": list(article["steps"]),
            "permission": article["permission"],
            "requires_human_confirmation": bool(article["requires_approval"]),
            "approval_hint": article["approval_hint"],
            "source": article["source"],
            "updated_at": article["updated_at"],
        }
