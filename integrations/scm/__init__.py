"""SCM-platform integration: client/auth/tools/policy_map.

LLM calls tools only through the Registry and PolicyEngine. Without
SCM_GATEWAY_URL, read tools use deterministic mocks for offline tests.
"""
from .tools import register_scm_tools

__all__ = ["register_scm_tools"]
