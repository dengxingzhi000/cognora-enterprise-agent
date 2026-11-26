import pytest

from security.quota import InMemoryQuotaRepository, QuotaExceeded, QuotaGuard


def test_tenant_rate_limit_isolated():
    guard = QuotaGuard(InMemoryQuotaRepository(), requests_per_minute=2)
    guard.check_request("t1")
    guard.check_request("t1")
    with pytest.raises(QuotaExceeded):
        guard.check_request("t1")
    guard.check_request("t2")


def test_token_budget_tracks_tenant_usage():
    guard = QuotaGuard(InMemoryQuotaRepository(), tokens_per_day=100)
    guard.consume_tokens("t1", 60)
    guard.consume_tokens("t1", 40)
    with pytest.raises(QuotaExceeded):
        guard.consume_tokens("t1", 1)
    guard.consume_tokens("t2", 100)
