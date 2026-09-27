import uuid

from fastapi.testclient import TestClient

from tests.conftest import Api


def test_create_and_fetch_account(api: Api, client: TestClient) -> None:
    created = api.account("wallet:alice")
    assert created["balance"] == 0
    assert created["version"] == 0
    assert created["type"] == "liability"

    fetched = client.get(f"/v1/accounts/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_duplicate_code_conflicts(api: Api, client: TestClient) -> None:
    api.account("wallet:alice")
    resp = client.post(
        "/v1/accounts",
        json={"code": "wallet:alice", "name": "dup", "type": "liability", "currency": "INR"},
    )
    assert resp.status_code == 409
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "conflict"


def test_idempotent_account_creation_replays(client: TestClient) -> None:
    body = {"code": "wallet:bob", "name": "Bob", "type": "liability", "currency": "INR"}
    headers = {"Idempotency-Key": "acct-bob-1"}
    first = client.post("/v1/accounts", json=body, headers=headers)
    second = client.post("/v1/accounts", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["Idempotent-Replayed"] == "true"


def test_validation_rejects_bad_input(client: TestClient) -> None:
    cases = [
        {"code": "Upper", "name": "x", "type": "asset", "currency": "INR"},
        {"code": "ok", "name": "x", "type": "asset", "currency": "inr"},
        {"code": "ok", "name": "x", "type": "cash", "currency": "INR"},
        {"code": "ok", "name": "", "type": "asset", "currency": "INR"},
        {"code": "ok", "name": "x", "type": "asset", "currency": "INR", "balance": 100},
        {"code": "ok", "name": "x", "type": "asset", "currency": "INR", "metadata": {"k": 1}},
    ]
    for body in cases:
        resp = client.post("/v1/accounts", json=body)
        assert resp.status_code == 422, body
        assert resp.json()["code"] == "validation_error"


def test_unknown_account_is_404(client: TestClient) -> None:
    resp = client.get(f"/v1/accounts/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_list_accounts_keyset_pagination(api: Api, client: TestClient) -> None:
    codes = [f"wallet:{i:02d}" for i in range(7)]
    for code in reversed(codes):
        api.account(code)

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/v1/accounts", params=params).json()
        seen.extend(a["code"] for a in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == codes


def test_invalid_cursor(client: TestClient) -> None:
    resp = client.get("/v1/accounts", params={"cursor": "%%%"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_cursor"


def test_auth_required(client: TestClient) -> None:
    assert client.get("/v1/accounts", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/v1/accounts", headers={"X-API-Key": "nope"}).status_code == 401
    assert (
        client.get("/v1/accounts", headers={"X-API-Key": "secondary-key-abcdef0123"}).status_code
        == 200
    )


def test_health_endpoints_are_public(client: TestClient) -> None:
    assert client.get("/healthz", headers={"X-API-Key": ""}).status_code == 200
    ready = client.get("/readyz", headers={"X-API-Key": ""})
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_request_id_propagates(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert resp.headers["X-Request-ID"] == "trace-123"
    generated = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert generated.headers["X-Request-ID"] != "bad id with spaces"
