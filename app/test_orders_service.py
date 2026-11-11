import orders_service as svc
from fastapi.testclient import TestClient

client = TestClient(svc.app)


def test_order_and_red_metrics():
    r = client.post("/orders", json={"sku": "ABC-1", "qty": 2})
    assert r.status_code == 201
    m = client.get("/metrics").text
    assert 'http_requests_total{code="201",method="POST",route="/orders",service="orders-service"}' in m
    assert "http_request_duration_seconds_bucket" in m
    assert "dependency_duration_seconds_count" in m


def test_route_label_uses_template_not_raw_path():
    client.get("/orders/123")
    client.get("/orders/456")
    m = client.get("/metrics").text
    assert 'route="/orders/{order_id}"' in m      # bounded label cardinality
    assert 'route="/orders/123"' not in m


def test_error_injection_counts_as_5xx():
    client.put("/admin/chaos", json={"latency_ms": 0, "error_rate": 1.0})
    try:
        assert client.post("/orders", json={"sku": "X"}).status_code == 503
        m = client.get("/metrics").text
        assert 'code="503"' in m
        assert 'chaos_injection{kind="error_rate",service="orders-service"} 1.0' in m
    finally:
        client.put("/admin/chaos", json={"latency_ms": 0, "error_rate": 0})


def test_probes_and_draining():
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    client.post("/admin/drain")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200   # still alive, just not taking traffic
    finally:
        svc.state["draining"] = False


def test_validation():
    assert client.post("/orders", json={"sku": "", "qty": 0}).status_code == 422
    assert client.put("/admin/chaos", json={"error_rate": 2}).status_code == 422
