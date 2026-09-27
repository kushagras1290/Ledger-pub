import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from ledger.models import Transaction
from tests.conftest import Api


def _txn_count(engine: Engine) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(Transaction)) or 0)


def test_retry_replays_original_response(api: Api, cash: dict[str, Any], engine: Engine) -> None:
    alice = api.account("wallet:alice")
    postings = [(cash["id"], "debit", 100), (alice["id"], "credit", 100)]

    first = api.post(postings, key="pay-1")
    second = api.post(postings, key="pay-1")

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["Idempotent-Replayed"] == "false"
    assert second.headers["Idempotent-Replayed"] == "true"
    assert api.balance(alice["id"]) == 100
    assert _txn_count(engine) == 1


def test_key_reuse_with_different_body_is_rejected(api: Api, cash: dict[str, Any]) -> None:
    alice = api.account("wallet:alice")
    assert (
        api.post([(cash["id"], "debit", 1), (alice["id"], "credit", 1)], key="k").status_code == 201
    )
    resp = api.post([(cash["id"], "debit", 2), (alice["id"], "credit", 2)], key="k")
    assert resp.status_code == 422
    assert resp.json()["code"] == "idempotency_key_reuse"


def test_failed_request_does_not_consume_key(api: Api, cash: dict[str, Any]) -> None:
    alice = api.account("wallet:alice")
    bob = api.account("wallet:bob")

    assert api.transfer(alice["id"], bob["id"], 50, key="t-1").status_code == 409
    api.fund(cash["id"], alice["id"], 50)
    retry = api.transfer(alice["id"], bob["id"], 50, key="t-1")
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "false"


def test_keys_are_scoped_per_operation(api: Api, cash: dict[str, Any]) -> None:
    alice = api.account("wallet:alice")
    bob = api.account("wallet:bob")
    funding = api.post([(cash["id"], "debit", 10), (alice["id"], "credit", 10)], key="shared")
    assert funding.status_code == 201
    assert api.transfer(alice["id"], bob["id"], 10, key="shared").status_code == 201


def test_concurrent_duplicates_execute_exactly_once(
    api: Api, cash: dict[str, Any], client: TestClient, engine: Engine
) -> None:
    alice = api.account("wallet:alice")
    key = uuid.uuid4().hex
    body = {
        "postings": [
            {"account_id": cash["id"], "direction": "debit", "amount": 777},
            {"account_id": alice["id"], "direction": "credit", "amount": 777},
        ]
    }

    def fire(_: int) -> tuple[int, str]:
        r = client.post("/v1/transactions", json=body, headers={"Idempotency-Key": key})
        return r.status_code, r.json()["id"]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(fire, range(32)))

    assert {status for status, _ in results} == {201}
    assert len({txn_id for _, txn_id in results}) == 1
    assert _txn_count(engine) == 1
    assert api.balance(alice["id"]) == 777
