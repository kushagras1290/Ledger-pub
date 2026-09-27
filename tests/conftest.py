import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from ledger.config import Settings
from ledger.main import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DATABASE_URL = os.environ.get(
    "LEDGER_TEST_DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5432/ledger_test"
)
API_KEY = "test-key-0123456789abcdef"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_db(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE postings, transactions, accounts, idempotency_records "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        api_keys=f"{API_KEY},secondary-key-abcdef0123",
        db_pool_size=20,
        db_max_overflow=40,
        max_write_retries=5,
        log_level="WARNING",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app, headers={"X-API-Key": API_KEY}) as c:
        yield c


class Api:
    """Thin helpers so tests read like ledger scenarios."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def account(
        self,
        code: str,
        type_: str = "liability",
        currency: str = "INR",
        allow_negative: bool = False,
    ) -> dict[str, Any]:
        resp = self.client.post(
            "/v1/accounts",
            json={
                "code": code,
                "name": code.replace(":", " ").title(),
                "type": type_,
                "currency": currency,
                "allow_negative": allow_negative,
            },
        )
        assert resp.status_code == 201, resp.text
        body: dict[str, Any] = resp.json()
        return body

    def post(
        self, postings: list[tuple[str, str, int]], key: str | None = None, **extra: Any
    ) -> Any:
        return self.client.post(
            "/v1/transactions",
            headers={"Idempotency-Key": key or uuid.uuid4().hex},
            json={
                "postings": [
                    {"account_id": a, "direction": d, "amount": amt} for a, d, amt in postings
                ],
                **extra,
            },
        )

    def transfer(self, src: str, dst: str, amount: int, key: str | None = None) -> Any:
        return self.client.post(
            "/v1/transfers",
            headers={"Idempotency-Key": key or uuid.uuid4().hex},
            json={"from_account_id": src, "to_account_id": dst, "amount": amount},
        )

    def reverse(self, txn_id: str, key: str | None = None) -> Any:
        return self.client.post(
            f"/v1/transactions/{txn_id}/reversal",
            headers={"Idempotency-Key": key or uuid.uuid4().hex},
        )

    def balance(self, account_id: str) -> int:
        resp = self.client.get(f"/v1/accounts/{account_id}/balance")
        assert resp.status_code == 200, resp.text
        value: int = resp.json()["balance"]
        return value

    def fund(self, cash_id: str, wallet_id: str, amount: int) -> Any:
        resp = self.post([(cash_id, "debit", amount), (wallet_id, "credit", amount)])
        assert resp.status_code == 201, resp.text
        return resp.json()

    def integrity(self) -> dict[str, Any]:
        resp = self.client.get("/v1/admin/integrity")
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


@pytest.fixture
def api(client: TestClient) -> Api:
    return Api(client)


@pytest.fixture
def cash(api: Api) -> dict[str, Any]:
    return api.account("cash:bank", type_="asset")
