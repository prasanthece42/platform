def test_public_payment_contract(client):
    response = client.post(
        "/payments", headers={"Idempotency-Key": "contract-order"}, json={"amount_cents": 100}
    )
    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")
    assert set(response.json()) == {
        "id",
        "amount_cents",
        "currency",
        "description",
        "status",
        "created_at",
    }
    assert response.json()["currency"] == "USD"
    assert client.get(response.headers["Location"]).json() == response.json()


def test_openapi_exposes_required_creation_header(client):
    schema = client.get("/openapi.json").json()
    parameters = schema["paths"]["/payments"]["post"]["parameters"]
    assert any(
        p["name"].lower() == "idempotency-key" and p["in"] == "header" and p["required"]
        for p in parameters
    )
    assert client.get("/docs").status_code == 200
