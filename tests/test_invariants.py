import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ledger.models import Posting, Transaction
from tests.conftest import Api


def _write_bypassing_service(engine: Engine, legs: list[tuple[str, str, int, str]]) -> None:
    """Insert a transaction straight through the ORM, skipping every service-level check."""
    txn_id = uuid.uuid4()
    with Session(engine) as s, s.begin():
        s.add(Transaction(id=txn_id))
        s.flush()
        s.add_all(
            Posting(
                transaction_id=txn_id,
                account_id=uuid.UUID(account_id),
                direction=direction,
                amount=amount,
                currency=currency,
                balance_after=amount,
                account_version=1,
            )
            for account_id, direction, amount, currency in legs
        )


def _execute(engine: Engine, stmt: str, **params: Any) -> None:
    with engine.begin() as conn:
        conn.execute(text(stmt), params)


def test_postings_and_transactions_are_append_only(
    api: Api, cash: dict[str, Any], engine: Engine
) -> None:
    alice = api.account("wallet:alice")
    api.fund(cash["id"], alice["id"], 100)

    for stmt in (
        "UPDATE postings SET amount = 1",
        "DELETE FROM postings",
        "UPDATE transactions SET description = 'x'",
        "DELETE FROM transactions",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            _execute(engine, stmt)


def test_database_rejects_unbalanced_writes(api: Api, cash: dict[str, Any], engine: Engine) -> None:
    alice = api.account("wallet:alice")
    legs = [(cash["id"], "debit", 100, "INR"), (alice["id"], "credit", 90, "INR")]
    with pytest.raises(IntegrityError, match="unbalanced"):
        _write_bypassing_service(engine, legs)


def test_database_rejects_single_legged_transactions(cash: dict[str, Any], engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="fewer than two"):
        _write_bypassing_service(engine, [(cash["id"], "debit", 1, "INR")])


def test_database_rejects_cross_currency_postings(
    api: Api, cash: dict[str, Any], engine: Engine
) -> None:
    alice = api.account("wallet:alice")
    legs = [(cash["id"], "debit", 1, "USD"), (alice["id"], "credit", 1, "USD")]
    with pytest.raises(IntegrityError, match="does not match"):
        _write_bypassing_service(engine, legs)


def test_integrity_report_detects_balance_drift(
    api: Api, cash: dict[str, Any], engine: Engine
) -> None:
    alice = api.account("wallet:alice")
    api.fund(cash["id"], alice["id"], 100)
    report = api.integrity()
    assert report["ok"] is True
    assert report["currencies"] == [
        {"currency": "INR", "debits": 100, "credits": 100, "balanced": True}
    ]

    _execute(
        engine,
        "UPDATE accounts SET posted_credits = posted_credits + 1 WHERE id = :id",
        id=alice["id"],
    )
    report = api.integrity()
    assert report["ok"] is False
    assert report["drifted_account_ids"] == [alice["id"]]
