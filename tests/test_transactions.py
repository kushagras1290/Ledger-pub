import time
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import Api


def test_funding_moves_both_sides(api: Api, cash: dict[str, Any]) -> None:
    wallet = api.account("wallet:alice")
    txn = api.fund(cash["id"], wallet["id"], 10_000)

    assert api.balance(cash["id"]) == 10_000
    assert api.balance(wallet["id"]) == 10_000
    assert {p["account_id"]: p["balance_after"] for p in txn["postings"]} == {
        cash["id"]: 10_000,
        wallet["id"]: 10_000,
    }
    assert api.integrity()["ok"] is True


def test_unbalanced_transaction_rejected_without_side_effects(
    api: Api, cash: dict[str, Any]
) -> None:
    wallet = api.account("wallet:alice")
    resp = api.post([(cash["id"], "debit", 100), (wallet["id"], "credit", 99)])
    assert resp.status_code == 422
    assert resp.json()["code"] == "unbalanced_transaction"
    assert resp.json()["imbalance"] == {"INR": 1}
    assert api.balance(cash["id"]) == 0


def test_multi_currency_must_balance_per_currency(api: Api) -> None:
    inr_cash = api.account("cash:inr", type_="asset")
    usd_cash = api.account("cash:usd", type_="asset", currency="USD")
    inr_wallet = api.account("wallet:inr")
    usd_wallet = api.account("wallet:usd", currency="USD")

    ok = api.post(
        [
            (inr_cash["id"], "debit", 8_300),
            (inr_wallet["id"], "credit", 8_300),
            (usd_cash["id"], "debit", 100),
            (usd_wallet["id"], "credit", 100),
        ]
    )
    assert ok.status_code == 201, ok.text

    bad = api.post([(inr_cash["id"], "debit", 8_300), (usd_wallet["id"], "credit", 8_300)])
    assert bad.status_code == 422
    assert bad.json()["imbalance"] == {"INR": 8_300, "USD": -8_300}


def test_unknown_and_duplicate_accounts(api: Api, cash: dict[str, Any]) -> None:
    ghost = "00000000-0000-4000-8000-000000000000"
    resp = api.post([(cash["id"], "debit", 1), (ghost, "credit", 1)])
    assert resp.status_code == 422
    assert resp.json()["code"] == "unknown_account"

    resp = api.post([(cash["id"], "debit", 1), (cash["id"], "credit", 1)])
    assert resp.status_code == 422
    assert resp.json()["code"] == "duplicate_posting_account"


def test_amounts_are_strict_positive_integers(api: Api, cash: dict[str, Any]) -> None:
    wallet = api.account("wallet:alice")
    for bad in (0, -5, 10.5, "10", 10**16):
        resp = api.post([(cash["id"], "debit", bad), (wallet["id"], "credit", bad)])  # type: ignore[arg-type]
        assert resp.status_code == 422, bad


def test_idempotency_key_required_for_money_movement(
    client: TestClient, cash: dict[str, Any]
) -> None:
    resp = client.post("/v1/transfers", json={})
    assert resp.status_code == 422


def test_insufficient_funds_is_atomic(api: Api, cash: dict[str, Any]) -> None:
    alice = api.account("wallet:alice")
    bob = api.account("wallet:bob")
    api.fund(cash["id"], alice["id"], 500)

    resp = api.transfer(alice["id"], bob["id"], 501)
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "insufficient_funds"
    assert body["available"] == 500
    assert api.balance(alice["id"]) == 500
    assert api.balance(bob["id"]) == 0


def test_allow_negative_accounts_can_overdraw(api: Api) -> None:
    equity = api.account("equity:capital", type_="equity", allow_negative=True)
    wallet = api.account("wallet:alice")
    resp = api.post([(equity["id"], "debit", 700), (wallet["id"], "credit", 700)])
    assert resp.status_code == 201
    assert api.balance(equity["id"]) == -700


def test_transfer_between_wallets(api: Api, cash: dict[str, Any]) -> None:
    alice = api.account("wallet:alice")
    bob = api.account("wallet:bob")
    api.fund(cash["id"], alice["id"], 1_000)

    resp = api.transfer(alice["id"], bob["id"], 250)
    assert resp.status_code == 201, resp.text
    directions = {p["account_id"]: p["direction"] for p in resp.json()["postings"]}
    assert directions == {alice["id"]: "debit", bob["id"]: "credit"}
    assert api.balance(alice["id"]) == 750
    assert api.balance(bob["id"]) == 250


def test_transfer_rejects_currency_and_side_mismatch(api: Api, cash: dict[str, Any]) -> None:
    inr = api.account("wallet:inr")
    usd = api.account("wallet:usd", currency="USD")
    resp = api.transfer(inr["id"], usd["id"], 1)
    assert resp.status_code == 422
    assert resp.json()["code"] == "currency_mismatch"

    resp = api.transfer(cash["id"], inr["id"], 1)
    assert resp.status_code == 422
    assert resp.json()["code"] == "incompatible_accounts"


def test_reversal_restores_balances_once(
    api: Api, cash: dict[str, Any], client: TestClient
) -> None:
    alice = api.account("wallet:alice")
    original = api.fund(cash["id"], alice["id"], 400)

    reversal = api.reverse(original["id"])
    assert reversal.status_code == 201, reversal.text
    rev = reversal.json()
    assert rev["reverses_id"] == original["id"]
    assert api.balance(alice["id"]) == 0
    assert api.balance(cash["id"]) == 0

    refreshed = client.get(f"/v1/transactions/{original['id']}").json()
    assert refreshed["reversed_by_id"] == rev["id"]

    again = api.reverse(original["id"])
    assert again.status_code == 409
    assert again.json()["code"] == "already_reversed"

    nested = api.reverse(rev["id"])
    assert nested.status_code == 409


def test_reversal_respects_funds(api: Api, cash: dict[str, Any]) -> None:
    alice = api.account("wallet:alice")
    bob = api.account("wallet:bob")
    funding = api.fund(cash["id"], alice["id"], 100)
    assert api.transfer(alice["id"], bob["id"], 100).status_code == 201

    resp = api.reverse(funding["id"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "insufficient_funds"


def test_reverse_unknown_transaction(api: Api) -> None:
    resp = api.reverse("00000000-0000-4000-8000-000000000000")
    assert resp.status_code == 404


def test_statement_is_paginated_newest_first(
    api: Api, cash: dict[str, Any], client: TestClient
) -> None:
    alice = api.account("wallet:alice")
    for amount in (100, 200, 300, 400, 500):
        api.fund(cash["id"], alice["id"], amount)

    lines: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get(f"/v1/accounts/{alice['id']}/statement", params=params).json()
        lines.extend(page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert [line["amount"] for line in lines] == [500, 400, 300, 200, 100]
    assert [line["balance_after"] for line in lines] == [1500, 1000, 600, 300, 100]


def test_point_in_time_balance(api: Api, cash: dict[str, Any], client: TestClient) -> None:
    alice = api.account("wallet:alice")
    api.fund(cash["id"], alice["id"], 100)
    time.sleep(0.01)
    checkpoint = datetime.now(UTC)
    time.sleep(0.01)
    api.fund(cash["id"], alice["id"], 50)

    past = client.get(
        f"/v1/accounts/{alice['id']}/balance", params={"as_of": checkpoint.isoformat()}
    ).json()
    assert past["balance"] == 100
    assert api.balance(alice["id"]) == 150


def test_version_counts_postings(api: Api, cash: dict[str, Any], client: TestClient) -> None:
    alice = api.account("wallet:alice")
    for _ in range(3):
        api.fund(cash["id"], alice["id"], 1)
    assert client.get(f"/v1/accounts/{alice['id']}").json()["version"] == 3
