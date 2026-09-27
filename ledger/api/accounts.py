import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ledger.api.deps import (
    OptionalIdempotencyKey,
    SessionDep,
    SessionFactoryDep,
    SettingsDep,
)
from ledger.api.responses import json_body, outcome_response
from ledger.idempotency import execute_idempotent
from ledger.schemas import AccountCreate, AccountOut, BalanceOut, Page, StatementLine
from ledger.service import LedgerService, account_out

router = APIRouter(prefix="/accounts", tags=["accounts"])

Limit = Annotated[int, Query(ge=1, le=200)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AccountOut)
def create_account(
    body: AccountCreate,
    factory: SessionFactoryDep,
    settings: SettingsDep,
    idempotency_key: OptionalIdempotencyKey = None,
) -> JSONResponse:
    def handler(session: Session) -> tuple[int, dict[str, object]]:
        account = LedgerService(session).create_account(body)
        return status.HTTP_201_CREATED, json_body(account_out(account))

    if idempotency_key is None:
        with factory() as session, session.begin():
            code, payload = handler(session)
        return JSONResponse(status_code=code, content=payload)

    return outcome_response(
        execute_idempotent(
            factory,
            scope="accounts.create",
            key=idempotency_key,
            payload=body.model_dump(mode="json"),
            handler=handler,
            max_attempts=settings.max_write_retries,
        )
    )


@router.get("", response_model=Page[AccountOut])
def list_accounts(
    session: SessionDep, limit: Limit = 50, cursor: str | None = None
) -> Page[AccountOut]:
    return LedgerService(session).list_accounts(limit, cursor)


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: uuid.UUID, session: SessionDep) -> AccountOut:
    return account_out(LedgerService(session).get_account(account_id))


@router.get("/{account_id}/balance", response_model=BalanceOut)
def get_balance(
    account_id: uuid.UUID, session: SessionDep, as_of: datetime | None = None
) -> BalanceOut:
    return LedgerService(session).balance(account_id, as_of)


@router.get("/{account_id}/statement", response_model=Page[StatementLine])
def get_statement(
    account_id: uuid.UUID, session: SessionDep, limit: Limit = 50, cursor: str | None = None
) -> Page[StatementLine]:
    return LedgerService(session).statement(account_id, limit, cursor)
