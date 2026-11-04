from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def _register(client: TestClient, slug: str) -> str:
    response = client.post(
        "/auth/register",
        json={
            "business_name": f"Retail {slug}",
            "tenant_slug": slug,
            "email": f"{slug}@example.com",
            "password": "Correct-Horse-Battery-42",
            "business_timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def test_retail_orders_are_tenant_scoped_idempotent_and_stock_safe() -> None:
    slug_a = f"retail-{uuid4().hex[:10]}"
    slug_b = f"retail-{uuid4().hex[:10]}"

    with TestClient(app) as client:
        token_a = _register(client, slug_a)
        token_b = _register(client, slug_b)
        auth_a = {"Authorization": f"Bearer {token_a}"}
        auth_b = {"Authorization": f"Bearer {token_b}"}

        product_response = client.post(
            "/products",
            headers=auth_a,
            json={
                "sku": "widget-01",
                "name": "Widget",
                "description": "Test inventory item",
                "price_minor": 250000,
                "stock_quantity": 1,
            },
        )
        assert product_response.status_code == 201
        product_id = product_response.json()["id"]
        assert product_response.json()["sku"] == "WIDGET-01"
        assert client.get("/products", headers=auth_b).json() == []

        first_key = str(uuid4())
        first_payload = {
            "idempotency_key": first_key,
            "customer_name": "Retail Customer",
            "customer_phone": "+2348012345678",
            "items": [{"product_id": product_id, "quantity": 1}],
        }
        first_order = client.post("/orders", headers=auth_a, json=first_payload)
        assert first_order.status_code == 201
        first_order_id = first_order.json()["id"]
        assert first_order.json()["total_minor"] == 250000
        assert first_order.json()["status"] == "pending_approval"

        replay = client.post("/orders", headers=auth_a, json=first_payload)
        assert replay.status_code == 201
        assert replay.json()["id"] == first_order_id

        changed_payload = dict(first_payload)
        changed_payload["items"] = [{"product_id": product_id, "quantity": 2}]
        conflict = client.post("/orders", headers=auth_a, json=changed_payload)
        assert conflict.status_code == 409

        second_payload = {
            "idempotency_key": str(uuid4()),
            "customer_name": "Second Customer",
            "customer_phone": "+2348098765432",
            "items": [{"product_id": product_id, "quantity": 1}],
        }
        second_order = client.post("/orders", headers=auth_a, json=second_payload)
        assert second_order.status_code == 201

        cross_tenant_approval = client.post(
            f"/orders/{first_order_id}/approve", headers=auth_b
        )
        assert cross_tenant_approval.status_code == 404

        approved = client.post(f"/orders/{first_order_id}/approve", headers=auth_a)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"

        out_of_stock = client.post(
            f"/orders/{second_order.json()['id']}/approve", headers=auth_a
        )
        assert out_of_stock.status_code == 409

        product_after_approval = client.get("/products", headers=auth_a)
        assert product_after_approval.json()[0]["stock_quantity"] == 0

        fulfilled = client.post(f"/orders/{first_order_id}/fulfill", headers=auth_a)
        assert fulfilled.status_code == 200
        assert fulfilled.json()["status"] == "fulfilled"
        assert client.get("/orders", headers=auth_b).json() == []
