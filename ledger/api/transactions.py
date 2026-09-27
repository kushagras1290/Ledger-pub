import uuid

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ledger.api.deps import IdempotencyKeyDep, SessionDep, SessionFactoryDep, SettingsDep
from ledger.api.responses import json_body, outcome_response
from ledger.idempotency import execute_idempotent
from ledger.schemas import ReversalCreate, TransactionCreate, TransactionOut, TransferCreate
from ledger.service import LedgerService, PostingSpec, transaction_out

router = APIRouter(tags=["transactions"])

Result = tuple[int, dict[str, object]]


@router.post("/transactions", status_code=status.HTTP_201_CREATED, response_model=TransactionOut)
def create_transaction(
    body: TransactionCreate,
    factory: SessionFactoryDep,
    idempotency_key: IdempotencyKeyDep,
    settings: SettingsDep,
) -> JSONResponse:
    def handler(session: Session) -> Result:
        txn = LedgerService(session).post_transaction(
            [PostingSpec(p.account_id, p.direction, p.amount) for p in body.postings],
            description=body.description,
            metadata=body.metadata,
        )
        return status.HTTP_201_CREATED, json_body(transaction_out(txn))

    return outcome_response(
        execute_idempotent(
            factory,
            scope="transactions.create",
            key=idempotency_key,
            payload=body.model_dump(mode="json"),
            handler=handler,
            max_attempts=settings.max_write_retries,
        )
    )


@router.post("/transfers", status_code=status.HTTP_201_CREATED, response_model=TransactionOut)
def create_transfer(
    body: TransferCreate,
    factory: SessionFactoryDep,
    idempotency_key: IdempotencyKeyDep,
    settings: SettingsDep,
) -> JSONResponse:
    def handler(session: Session) -> Result:
        txn = LedgerService(session).transfer(
            body.from_account_id,
            body.to_account_id,
            body.amount,
            body.description,
            body.metadata,
        )
        return status.HTTP_201_CREATED, json_body(transaction_out(txn))

    return outcome_response(
        execute_idempotent(
            factory,
            scope="transfers.create",
            key=idempotency_key,
            payload=body.model_dump(mode="json"),
            handler=handler,
            max_attempts=settings.max_write_retries,
        )
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionOut)
def get_transaction(transaction_id: uuid.UUID, session: SessionDep) -> TransactionOut:
    return transaction_out(LedgerService(session).get_transaction(transaction_id))


@router.post(
    "/transactions/{transaction_id}/reversal",
    status_code=status.HTTP_201_CREATED,
    response_model=TransactionOut,
)
def reverse_transaction(
    transaction_id: uuid.UUID,
    factory: SessionFactoryDep,
    idempotency_key: IdempotencyKeyDep,
    settings: SettingsDep,
    body: ReversalCreate | None = None,
) -> JSONResponse:
    description = body.description if body is not None else None

    def handler(session: Session) -> Result:
        txn = LedgerService(session).reverse_transaction(transaction_id, description)
        return status.HTTP_201_CREATED, json_body(transaction_out(txn))

    return outcome_response(
        execute_idempotent(
            factory,
            scope="transactions.reverse",
            key=idempotency_key,
            payload={"transaction_id": str(transaction_id), "description": description},
            handler=handler,
            max_attempts=settings.max_write_retries,
        )
    )
