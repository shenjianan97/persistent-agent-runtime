def test_3_14_health_endpoint(e2e):
    """3.14 Health endpoint reports service and DB readiness."""
    body = e2e.api.health()["body"]

    assert body["status"] == "healthy"
    assert body["database"] == "connected"
    assert isinstance(body["queued_tasks"], int)
    assert isinstance(body["active_workers"], int)
    assert body["queued_tasks"] >= 0
    assert body["active_workers"] >= 0
