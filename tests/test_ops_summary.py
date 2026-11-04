from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def test_operations_summary_requires_platform_key_and_returns_aggregate_data(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_KEY", "test-operations-key-long-enough")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            unauthorized = client.get("/internal/ops/summary")
            assert unauthorized.status_code == 401

            summary = client.get(
                "/internal/ops/summary",
                headers={"X-Platform-Admin-Key": "test-operations-key-long-enough"},
            )
            assert summary.status_code == 200
            payload = summary.json()
            assert set(payload) == {
                "generated_at",
                "jobs",
                "workers",
                "outbound_messages",
                "webhook_events",
                "whatsapp_tokens_expiring_within_7_days",
            }
            assert set(payload["jobs"]) == {
                "queued",
                "running",
                "completed",
                "dead_letter",
                "oldest_queued_at",
                "oldest_queued_age_seconds",
            }
            assert isinstance(payload["workers"], list)
            assert isinstance(payload["outbound_messages"], dict)
            assert isinstance(payload["webhook_events"], dict)
            serialized = str(payload).casefold()
            assert "access_token" not in serialized
            assert "phone_number" not in serialized
            assert "customer_phone" not in serialized
    finally:
        get_settings.cache_clear()
