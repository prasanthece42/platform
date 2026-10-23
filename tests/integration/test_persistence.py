from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from payments_api.main import create_app


def test_payment_and_idempotency_survive_app_restart(tmp_path):
    path = tmp_path / "ledger.db"
    payload = {"amount_cents": 1200, "currency": "USD"}
    headers = {"Idempotency-Key": "persisted-order"}
    with TestClient(create_app(path)) as first:
        payment = first.post("/payments", headers=headers, json=payload).json()
    with TestClient(create_app(path)) as restarted:
        assert restarted.get(f"/payments/{payment['id']}").json() == payment
        retry = restarted.post("/payments", headers=headers, json=payload)
        assert retry.status_code == 200 and retry.json() == payment


def test_concurrent_retries_create_one_record(client):
    def submit(_):
        return client.post(
            "/payments",
            headers={"Idempotency-Key": "concurrent-order"},
            json={"amount_cents": 1200},
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(submit, range(10)))
    assert sum(response.status_code == 201 for response in responses) == 1
    assert all(response.status_code in {200, 201} for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(client.get("/payments").json()) == 1
