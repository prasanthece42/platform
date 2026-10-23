import pytest


def create(client, key="order-001", **changes):
    return client.post(
        "/payments",
        headers={"Idempotency-Key": key},
        json={"amount_cents": 2500, "currency": "CAD", "description": "Demo order", **changes},
    )


def test_create_list_get_and_cancel(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/payments").json() == []
    response = create(client)
    assert response.status_code == 201
    payment = response.json()
    assert payment["amount_cents"] == 2500 and payment["status"] == "recorded"
    assert "idempotency_key" not in payment
    location = response.headers["Location"]
    assert client.get(location).json() == payment
    assert client.get("/payments").json() == [payment]
    cancelled = client.post(location + "/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(location + "/cancel").json() == cancelled.json()
    assert client.get(location).json()["status"] == "cancelled"


def test_retries_do_not_create_duplicate_records(client):
    first = create(client)
    repeated = create(client)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    conflict = create(client, amount_cents=3000)
    assert conflict.status_code == 409
    assert len(client.get("/payments").json()) == 1
    client.post(first.headers["Location"] + "/cancel")
    assert create(client).json()["status"] == "cancelled"


@pytest.mark.parametrize("amount", [0, -1, 1.5, "2500", True, 100_000_001])
def test_invalid_amounts_are_rejected(client, amount):
    assert create(client, amount_cents=amount).status_code == 422
    assert client.get("/payments").json() == []


@pytest.mark.parametrize(
    "changes",
    [{"currency": "XYZ"}, {"currency": "usd"}, {"description": "x" * 201}, {"unexpected": "field"}],
)
def test_invalid_payment_details_are_rejected(client, changes):
    assert create(client, **changes).status_code == 422


def test_missing_records_and_invalid_queries(client):
    assert client.get("/payments/unknown").status_code == 404
    assert client.post("/payments/unknown/cancel").status_code == 404
    assert client.get("/payments?limit=0").status_code == 422
    assert client.get("/payments?limit=101").status_code == 422
    assert client.post("/payments", json={"amount_cents": 100}).status_code == 422
    assert create(client, key="bad key").status_code == 422


def test_listing_is_bounded(client):
    create(client, key="first")
    second = create(client, key="second").json()
    assert client.get("/payments?limit=1").json() == [second]
